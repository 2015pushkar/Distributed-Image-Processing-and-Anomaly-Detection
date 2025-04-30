import argparse
import pathlib
import pandas as pd
import matplotlib.pyplot as plt

# -----------------------------------------------------------------------------
# perf_plots.py – Generate scalability‑performance graphs
# -----------------------------------------------------------------------------
# Expected columns in the input CSV / JSON:
#   workers      – number of worker nodes (or executors)
#   cores        – total CPU cores
#   data_gb      – dataset size processed in GiB
#   runtime_sec  – wall‑clock duration of the job in seconds
#
# Example CSV:
#     workers,cores,data_gb,runtime_sec
#     2,8,8.5,900
#     4,16,8.5,510
#     8,32,8.5,280
#     4,16,16.0,950
#     4,16,32.0,1800
#
# Usage:
#     python perf_plots.py results.csv --outdir plots
# -----------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate perf graphs from benchmark results")
    parser.add_argument("input", help="CSV or JSON file with experimental data")
    parser.add_argument("--outdir", default="plots", help="Directory to write PNGs")
    args = parser.parse_args(argv)

    outdir = pathlib.Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    inp_path = pathlib.Path(args.input)
    if inp_path.suffix.lower() == ".json":
        df = pd.read_json(inp_path)
    else:
        df = pd.read_csv(inp_path)

    df.columns = [c.lower() for c in df.columns]
    required = {"workers", "data_gb", "runtime_sec"}
    if not required.issubset(df.columns):
        raise ValueError(f"Input file must contain columns {required}")
    if "cores" not in df.columns:
        df["cores"] = df["workers"]

    def save(fig, name):
        fig.tight_layout()
        fig.savefig(outdir / name, dpi=150)

    # Runtime vs workers
    fig1 = plt.figure()
    for size, grp in df.groupby("data_gb"):
        g = grp.sort_values("workers")
        plt.plot(g["workers"], g["runtime_sec"] / 60, marker="o", label=f"{size} GiB")
    plt.xlabel("Workers")
    plt.ylabel("Runtime (minutes)")
    plt.title("Job duration vs number of workers")
    plt.grid(True, linestyle=":", alpha=0.4)
    plt.legend()
    save(fig1, "job_time_vs_workers.png")

    # Runtime vs data size
    fig2 = plt.figure()
    for workers, grp in df.groupby("workers"):
        g = grp.sort_values("data_gb")
        plt.plot(g["data_gb"], g["runtime_sec"] / 60, marker="s", label=f"{workers} workers")
    plt.xlabel("Dataset size (GiB)")
    plt.ylabel("Runtime (minutes)")
    plt.title("Job duration vs data volume")
    plt.grid(True, linestyle=":", alpha=0.4)
    plt.legend()
    save(fig2, "job_time_vs_data.png")

    print(f"Graphs written to {outdir.resolve()}/")


if __name__ == "__main__":
    main()
