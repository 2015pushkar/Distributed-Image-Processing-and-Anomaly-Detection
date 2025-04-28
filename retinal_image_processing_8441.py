from pyspark.sql.functions import col
from pyspark.sql.types import StringType, StructType, StructField
from pyspark.sql import SparkSession
from typing import Iterator
import pandas as pd
import cv2
import numpy as np
import io, tarfile, uuid
from google.cloud import storage
import traceback

spark = (
    SparkSession.builder
        # Bigger Arrow batches == fewer Python<->JVM hand‑offs
        .config("spark.sql.execution.arrow.maxRecordsPerBatch", "1024")
        .config("spark.executor.memory", "2g")
        .config("spark.executor.memoryOverhead", "768m")
        # GCS connector performance knobs
        .config("spark.hadoop.fs.gs.inputstream.min.range.request.size", "8388608")
        .config("spark.hadoop.fs.gs.performance.cache.enable", "true")
        .config("spark.hadoop.fs.gs.performance.cache.max.entry.age.ms", "120000")
        .getOrCreate()
)

# Output schema definition
output_schema = StructType([
    StructField("batch_output_path", StringType(), True)
])

# Constants for tar splitting and upload tuning
MAX_TAR_BYTES = 256 * 1024 * 1024   # flush archive every 256 MiB
CHUNK_SIZE = 8 * 1024 * 1024    # 8 MiB resumable‑upload chunks

# Partition‑level processing UDF
def process_upload_iter(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    """MapInPandas func – one *Spark partition* at a time.

    Adds live tar‑splitting: once the in‑memory archive exceeds ``MAX_TAR_BYTES``
    we flush it to GCS and start a new tar, yielding *multiple* uploads per
    partition while still keeping each HTTP stream fast.
    """
    client      = storage.Client()
    dst_bucket  = client.bucket("retinal_images")

    # Re‑usable CV objects
    clahe = cv2.createCLAHE(clipLimit=4.0)
    k1 = np.ones((1, 1), np.uint8)
    k2 = np.ones((2, 2), np.uint8)

    for pdf in batches:
        tar_buf = io.BytesIO()
        tar = tarfile.open(mode="w", fileobj=tar_buf)
        current_bytes = 0
        uploaded_paths = []

        try:
            for path, raw_bytes in zip(pdf["path"], pdf["content"]):
                try:
                    arr = cv2.imdecode(np.asarray(memoryview(raw_bytes), dtype=np.uint8), cv2.IMREAD_COLOR)
                    if arr is None:
                        continue
                    b, g, r = cv2.split(arr)
                    g = clahe.apply(g)
                    gray = cv2.cvtColor(cv2.merge((b, g, r)), cv2.COLOR_BGR2GRAY)
                    mo = cv2.morphologyEx(gray, cv2.MORPH_OPEN, k1)
                    thr = cv2.adaptiveThreshold(mo, 255,
                                                cv2.ADAPTIVE_THRESH_MEAN_C,
                                                cv2.THRESH_BINARY_INV,
                                                9, 5)
                    mo2 = cv2.morphologyEx(thr, cv2.MORPH_OPEN, k2)
                    stacked = np.stack((mo2,)*3, axis=-1)
                    ok, enc = cv2.imencode(".jpeg", stacked)
                    if not ok:
                        continue

                    _, _, name = path.partition("gs://retinal_images/sample/")
                    ti = tarfile.TarInfo(name=f"{name}.jpeg")
                    ti.size = enc.size
                    ti.mtime = int(pd.Timestamp.utcnow().timestamp())
                    tar.addfile(ti, io.BytesIO(enc.tobytes()))
                    current_bytes += enc.size

                    # Flush archive if it grows beyond threshold
                    if current_bytes >= MAX_TAR_BYTES:
                        tar.close()
                        tar_buf.seek(0)
                        batch_key = f"output_8k/batch_{uuid.uuid4().hex}.tar"
                        blob = dst_bucket.blob(batch_key)
                        blob.chunk_size = CHUNK_SIZE
                        blob.upload_from_file(
                            tar_buf,
                            size=tar_buf.getbuffer().nbytes,
                            content_type="application/x-tar"
                        )
                        uploaded_paths.append(f"gs://{dst_bucket.name}/{batch_key}")
                        # Reset for next slice
                        tar_buf = io.BytesIO()
                        tar = tarfile.open(mode="w", fileobj=tar_buf)
                        current_bytes = 0
                except Exception as img_proc_e:
                    print(f"ERROR processing image {path}: {img_proc_e}\n{traceback.format_exc()}")

            # Flush remainder (if any)
            tar.close()
            tar_buf.seek(0)
            if tar_buf.getbuffer().nbytes:
                batch_key = f"output_8k/batch_{uuid.uuid4().hex}.tar"
                blob = dst_bucket.blob(batch_key)
                blob.chunk_size = CHUNK_SIZE
                blob.upload_from_file(
                    tar_buf,
                    size=tar_buf.getbuffer().nbytes,
                    content_type="application/x-tar"
                )
                uploaded_paths.append(f"gs://{dst_bucket.name}/{batch_key}")

            upload_result = ";".join(uploaded_paths) if uploaded_paths else "SKIPPED:empty_partition"

        except Exception as e:
            print(f"Error {e}\n{traceback.format_exc()}")
            upload_result = f"Error:{e.__class__.__name__}"

        # yield one‑row DF per partition
        yield pd.DataFrame({"batch_output_path": [upload_result]})

# Read and process data
source_df = (
    spark.read.format("binaryFile")
         .option("recursiveFileLookup", "true")
         .load("gs://retinal_images/sample/")
         .select("path", "content")
         # 8× CPU count to keep I/O pipeline full
         .repartition(spark.sparkContext.defaultParallelism * 8)
)

print(f"Total images discovered: {source_df.count()}")

uploaded_info = source_df.mapInPandas(process_upload_iter, schema=output_schema)

print(f"Batches processed: {uploaded_info.count()}")

