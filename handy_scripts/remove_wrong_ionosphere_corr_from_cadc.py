"""
Query, audit, and optionally remove POSSUM data products from CADC.

This script performs a two-stage cleanup workflow against the CIRADA
CADC TAP service:

1. Artifact removal
   - Queries caom2.Observation, caom2.Plane, and caom2.Artifact for POSSUM.
   - Applies optional filters on tile number, frequency band, and a
     lastModified cutoff date.
   - Produces a diagnostic plot of ingestion counts per day.
   - Optionally deletes matching artifacts using the cadcremove CLI.
   - Always logs targeted artifact URIs for traceability.

2. Metadata removal
   - Queries caom2.Observation and caom2.Plane to obtain observationIDs.
   - Writes a todo file (one observationID per line).
   - Optionally runs caom2-repo visit with a user-supplied plugin to
     remove CAOM2 metadata corresponding to the deleted artifacts.

Safety and usage notes:
- By default, this script deletes products last modified before 2025-01-01.
- By default, the script runs in dry-run mode: no deletions are executed.
  Only logs and todo.txt are written.
- Actual deletion requires explicitly disabling dry-run (for example,
  via a command-line flag).
- It is strongly recommended to inspect the generated logs and todo.txt
  before performing irreversible operations.
- Authentication relies on a valid CADC proxy certificate (cadcproxy.pem).

Intended use:
- Cleanup of historical POSSUM products from CADC, e.g. those with incorrect ionosphere corrections.


This script has been ran on 2025-01-14 to delete wrong data from CADC. Results are found in

/arc/projects/CIRADA/polarimetry/tests/remove_wrong_ionosphere_from_cadc/
"""

import argparse
import os
import subprocess
from io import StringIO
from pathlib import Path

import astroquery.cadc as cadc
import matplotlib.pyplot as plt
import pandas as pd
from astropy.table import Table
from cadctap import CadcTapClient
from cadcutils.net import Subject


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _plot_counts_per_day(days: pd.Series, outpath: Path) -> None:
    counts_per_day = days.value_counts().sort_index()

    plt.figure()
    plt.scatter(counts_per_day.index, counts_per_day.values)
    plt.xlabel("Day of ingestion")
    plt.ylabel("Number of artifacts ingested")
    plt.tight_layout()

    _ensure_parent_dir(outpath)
    plt.savefig(outpath.as_posix())
    plt.close()


def _run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    """
    Returns (returncode, stdout, stderr).
    """
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _write_log_line(log_path: Path, line: str) -> None:
    _ensure_parent_dir(log_path)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _run_cadcremove(artifact_uri: str, cert_file: str) -> tuple[int, str, str]:
    cmd = ["cadcremove", "--cert", cert_file, artifact_uri]
    return _run_cmd(cmd)


def _make_cadc_session(cert_file: str) -> cadc.Cadc:
    os.environ["CADCTAP_SERVICE_URI"] = "ivo://cadc.nrc.ca/ams/cirada"
    session = cadc.Cadc()
    session.login(certificate_file=cert_file)
    return session


def remove_metadata(
    cert_file: str,
    todo_path: str = "todo.txt",
    plugin_path: str = "./remove_artifact.py",
    collection: str = "POSSUM",
    dry_run: bool = True,
    metadata_log_path: str = "caom2_repo_visit.log",
    metadata_error_log_path: str = "caom2_repo_visit_errors.log",
) -> None:
    """
    Run (or dry-run) CAOM2 metadata removal using caom2-repo visit.

    Command executed when dry_run is False:
      caom2-repo visit --cert cert_file --obs_file todo.txt --plugin ./remove_artifact.py POSSUM

    If dry_run is True, this function only checks that todo.txt exists and logs what it would do.
    """
    todo_p = Path(todo_path)
    if not todo_p.exists():
        raise FileNotFoundError(f"todo file not found: {todo_p.resolve()}")

    cmd = [
        "caom2-repo",
        "visit",
        "--cert",
        cert_file,
        "--obs_file",
        todo_p.as_posix(),
        "--plugin",
        plugin_path,
        collection,
    ]

    _write_log_line(Path(metadata_log_path), f"# cmd: {' '.join(cmd)}")
    _write_log_line(
        Path(metadata_log_path), f"# dry_run={dry_run} todo={todo_p.resolve()}"
    )

    if dry_run:
        print(
            f"Dry run: wrote/using {todo_p.resolve()} but will not execute caom2-repo."
        )
        return
    else:
        print(f"Running caom2-repo visit to remove metadata for {todo_p.resolve()}")

    rc, out, err = _run_cmd(cmd)
    if rc != 0:
        _write_log_line(
            Path(metadata_error_log_path),
            f"returncode: {rc}\nstdout:\n{out}\nstderr:\n{err}\n---",
        )
        raise RuntimeError(
            f"caom2-repo visit failed (rc={rc}). See {Path(metadata_error_log_path).resolve()}"
        )

    # Keep stdout for auditability
    if out.strip():
        _write_log_line(Path(metadata_log_path), out.strip())

    print("Metadata removal complete via caom2-repo visit.")


def _write_todo_file(observation_ids: list[str], todo_path: Path) -> None:
    _ensure_parent_dir(todo_path)
    # Ensure unique, stable order
    unique_ids = sorted(set(observation_ids))
    with todo_path.open("w", encoding="utf-8") as f:
        for obs_id in unique_ids:
            f.write(f"{obs_id}\n")

def _run_query(
    query_string,
    client,
    collection,
    timeout=60,
) -> Table:
    # timeout is in minutes
    print('Starting query for POSSUM data records. This may take a while...')
    buffer = StringIO()
    client.query(
        query_string,
        output_file=buffer,
        data_only=True,
        response_format='tsv',
        timeout=timeout,
    )
    temp = Table.read(buffer.getvalue().split('\n'), format='ascii.tab')
    print('Query finished')
    temp.to_pandas().to_csv(f'./{collection}_all_file_uris.txt', index=False, header=False)
    return temp

def query_possum_files():
    """Query all POSSUM files (data records) from CADC.
    
    return as an astropy Table
    """
    collection = "POSSUM"
    resource_id = "ivo://cadc.nrc.ca/global/luskan"
    certificate = Path("~/.ssl/cadcproxy.pem").expanduser()
    assert certificate.exists(), f"Certificate file not found: {certificate.resolve()}. Please run cadc-get-cert"
    
    subject = Subject(certificate=str(certificate))

    qs = f"""
    SELECT A.uri, A.lastModified
    FROM inventory.Artifact AS A
    WHERE A.uri LIKE '%:{collection}/%'
    """
    client = CadcTapClient(subject, resource_id=resource_id)
    result_table = _run_query(qs, client, collection)
    return result_table


def remove_observation(
    cert_file: str,
    todo_path: str = "todo.txt",
    collection: str = "POSSUM",
    dry_run: bool = True,
    log_path: str = "caom2_repo_visit.log",
    error_log_path: str = "caom2_repo_visit_errors.log",
) -> None:
    """
    Run removal of entire observations using caom2-repo delete.

    example command executed when dry_run is False:
      caom2-repo delete --cert cert_file POSSUM observationID

    e.g.

    caom2-repo delete --cert ~/.ssl/cadcproxy.pem POSSUM 1367MHz_20asec_0900-0112_6256_v1
    """

    todo_p = Path(todo_path)
    if not todo_p.exists():
        raise FileNotFoundError(f"todo file not found: {todo_p.resolve()}")

    with todo_p.open("r", encoding="utf-8") as f:
        observation_ids = [line.strip() for line in f if line.strip()]

    _write_log_line(Path(log_path), f"# Start: dry_run={dry_run} todo={todo_p.resolve()}")

    for obs_id in observation_ids:
        cmd = [
            "caom2-repo",
            "delete",
            "--cert",
            cert_file,
            collection,
            obs_id,
        ]

        _write_log_line(Path(log_path), f"# cmd: {' '.join(cmd)}")

        if dry_run:
            continue

        rc, out, err = _run_cmd(cmd)
        if rc != 0:
            _write_log_line(
                Path(error_log_path),
                f"observationID: {obs_id}\nreturncode: {rc}\nstdout:\n{out}\nstderr:\n{err}\n---",
            )
            print(
                f"caom2-repo delete failed for observationID={obs_id} (rc={rc}). See {Path(error_log_path).resolve()}"
            )

        # Keep stdout for auditability
        if out.strip():
            _write_log_line(Path(log_path), out.strip())

    print("Observation removal complete via caom2-repo delete.")


def check_and_remove_from_CADC(
    tilenumber: str | None,
    band: None | str = "943MHz",
    cadc_cert_file: str | None = None,
    cutoff_date: str = "2025-01-01",
    dry_run: bool = True,
    plot_path: str = "CADC_ingestion_counts_per_day.png",
    uri_log_path: str = "cadcremove_uris.log",
    uri_error_log_path: str = "cadcremove_errors.log",
    todo_path: str = "todo.txt",
    plugin_path: str = "./remove_artifact.py",
    metadata_log_path: str = "caom2_repo_visit.log",
    metadata_error_log_path: str = "caom2_repo_visit_errors.log",
) -> pd.DataFrame:
    """
    1) Query POSSUM artifacts (Observation/Plane/Artifact) and optionally delete them with cadcremove.
    2) Build a todo file of observationIDs (using the simpler Plane+Observation query),
       and if not dry_run, run "caom2-repo visit" to remove metadata via plugin.

    Returns a pandas DataFrame of artifacts selected for deletion (or dry-run deletion).
    """
    if cadc_cert_file is None:
        cadc_cert_file = os.path.expanduser("~/.ssl/cadcproxy.pem")
    session = _make_cadc_session(cadc_cert_file)

    # ---- Query 1: Artifact URIs for data records / files (for cadcremove) ----
    art_table = query_possum_files()

    # # Add some derived columns for debugging/filtering
    # tile_numbers = [x.split("_")[-2] for x in art_table["observationID"]]
    # freqs = [x.split("MHz")[0] for x in art_table["observationID"]]
    # art_table.add_column(tile_numbers, name="tile_number")
    # art_table.add_column(freqs, name="freq")

    # base_mask = pd.Series([True] * len(art_table))
    # if band is None:
    #     target_freq = None
    # else:
    #     target_freq = band.replace("MHz", "")

    #     base_mask &= pd.Series([str(f) == str(target_freq) for f in art_table["freq"]])

    # if tilenumber is not None:
    #     base_mask &= pd.Series(
    #         [str(t) == str(tilenumber) for t in art_table["tile_number"]]
    #     )

    # art_table = art_table[base_mask.values]

    # lastModified -> pandas datetime
    dt = pd.to_datetime(art_table["lastModified"], utc=True)
    days = dt.floor("D")
    _plot_counts_per_day(days, Path(plot_path))

    cutoff = pd.Timestamp(cutoff_date, tz="UTC")
    old_mask = dt < cutoff
    subtable = art_table[old_mask]
    subdf = subtable.to_pandas()

    print(
        f"Matched {len(art_table)} artifacts after filters (band={band}, tile={tilenumber})."
    )
    print(
        f"Will {'DRY-RUN delete' if dry_run else 'delete'} {len(subdf)} artifacts lastModified before {cutoff}."
    )

    subdf["artifactURI"] = subdf['uri']

    # cadcremove phase
    _write_log_line(
        Path(uri_log_path),
        f"# Start: cutoff={cutoff.isoformat()} band={band} tile={tilenumber} dry_run={dry_run}",
    )

    deleted = 0
    failed = 0
    for _, row in subdf.iterrows():
        artifact_uri = str(row["artifactURI"])
        _write_log_line(Path(uri_log_path), artifact_uri)

        if dry_run:
            continue

        rc, out, err = _run_cadcremove(artifact_uri, cadc_cert_file)
        if rc == 0:
            deleted += 1
        else:
            failed += 1
            _write_log_line(
                Path(uri_error_log_path),
                f"URI: {artifact_uri}\nreturncode: {rc}\nstdout:\n{out}\nstderr:\n{err}\n---",
            )

    if not dry_run:
        print(f"cadcremove done. Deleted={deleted}, Failed={failed}.")
        if failed > 0:
            print(f"See errors in: {Path(uri_error_log_path).resolve()}")

    # ---- Query 2: observationID list (for caom2-repo visit) ----
    # for getting the metadata
    q_obs = session.create_async(
        """
        SELECT
            O.observationID,
            P.productID,
            O.lastModified,
            P.planeURI,
            A.uri AS artifactURI
        FROM caom2.Observation AS O
        JOIN caom2.Plane AS P
            ON O.obsID = P.obsID
        JOIN caom2.Artifact AS A
            ON P.planeID = A.planeID
        WHERE
            O.collection = 'POSSUM'
            AND O.observationID NOT LIKE '%pilot1'
        """
    )
    print('Starting query for POSSUM metadata records. This may take a while...')

    q_obs.run().wait()
    q_obs.raise_if_error()
    obs_table = q_obs.fetch_result().to_table()

    # Apply the same tile/band restriction and cutoff to observationIDs, so metadata removal matches deletions.
    obs_tile_numbers = [x.split("_")[-2] for x in obs_table["observationID"]]
    obs_freqs = [x.split("MHz")[0] for x in obs_table["observationID"]]
    obs_table.add_column(obs_tile_numbers, name="tile_number")
    obs_table.add_column(obs_freqs, name="freq")

    obs_dt = pd.to_datetime(obs_table["lastModified"].filled(pd.NA), utc=True)

    obs_mask = pd.Series([True] * len(obs_table))
    
    if band is not None:
        target_freq = band.replace("MHz", "")
        obs_mask &= pd.Series([str(f) == str(target_freq) for f in obs_table["freq"]])
    
    if tilenumber is not None:
        obs_mask &= pd.Series(
            [str(t) == str(tilenumber) for t in obs_table["tile_number"]]
        )
    obs_mask &= obs_dt < cutoff

    obs_selected = obs_table[obs_mask.values]
    observation_ids = [str(x) for x in obs_selected["observationID"]]

    todo_p = Path(todo_path)
    _write_todo_file(observation_ids, todo_p)
    print(
        f"Wrote {len(sorted(set(observation_ids)))} observationIDs to {todo_p.resolve()}"
    )

    # Remove metadata (or dry-run)
    # remove_metadata(
    #     cert_file=cadc_cert_file,
    #     todo_path=todo_p.as_posix(),
    #     plugin_path=plugin_path,
    #     collection="POSSUM",
    #     dry_run=dry_run,
    #     metadata_log_path=metadata_log_path,
    #     metadata_error_log_path=metadata_error_log_path,
    # )


    # Remove entire observation from CADC
    remove_observation(
        cert_file=cadc_cert_file,
        todo_path=todo_p.as_posix(),
        collection="POSSUM",
        dry_run=dry_run,
        log_path=metadata_log_path.replace('visit', 'delete'),
        error_log_path=metadata_error_log_path.replace('visit', 'delete')
    )

    return subdf




def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query CADC and optionally delete POSSUM artifacts + metadata."
    )
    parser.add_argument(
        "--tile",
        dest="tile",
        default=None,
        help="Tile number to restrict to (default: no restriction).",
    )
    parser.add_argument(
        "--band", dest="band", default=None,#"943MHz", 
        help="Band string like 943MHz. Or None for no restriction."
    )
    parser.add_argument(
        "--cert",
        dest="cert",
        default=os.path.expanduser("~/.ssl/cadcproxy.pem"),
        help="Path to CADC proxy certificate PEM.",
    )
    parser.add_argument(
        "--cutoff",
        dest="cutoff",
        default="2025-01-01",
        help="Cutoff date (UTC) in YYYY-MM-DD; targets records lastModified before this.",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Do not delete; only log URIs and write todo.txt (recommended first).",
    )
    parser.add_argument(
        "--do-delete",
        dest="do_delete",
        action="store_true",
        help="Actually run cadcremove and caom2-repo visit.",
    )
    parser.add_argument(
        "--plot",
        dest="plot",
        default="CADC_ingestion_counts_per_day.png",
        help="Output path for plot.",
    )
    parser.add_argument(
        "--uri-log",
        dest="uri_log",
        default="cadcremove_uris.log",
        help="Log file for URIs targeted.",
    )
    parser.add_argument(
        "--uri-error-log",
        dest="uri_error_log",
        default="cadcremove_errors.log",
        help="Log file for cadcremove failures.",
    )
    parser.add_argument(
        "--todo",
        dest="todo",
        default="todo.txt",
        help="todo.txt path (one observationID per line).",
    )
    parser.add_argument(
        "--plugin",
        dest="plugin",
        default="/arc/projects/CIRADA/polarimetry/software/POSSUMutils/handy_scripts/remove_artifact.py",
        help="Plugin path for caom2-repo visit.",
    )
    parser.add_argument(
        "--meta-log",
        dest="meta_log",
        default="caom2_repo_visit.log",
        help="Log for caom2-repo output.",
    )
    parser.add_argument(
        "--meta-error-log",
        dest="meta_error_log",
        default="caom2_repo_visit_errors.log",
        help="Log for caom2-repo failures.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Default behavior: dry-run unless --do-delete is passed.
    dry_run = True
    if args.do_delete:
        dry_run = False
    elif args.dry_run:
        dry_run = True

    check_and_remove_from_CADC(
        tilenumber=args.tile,
        band=args.band,
        cadc_cert_file=args.cert,
        cutoff_date=args.cutoff,
        dry_run=dry_run,
        plot_path=args.plot,
        uri_log_path=args.uri_log,
        uri_error_log_path=args.uri_error_log,
        todo_path=args.todo,
        plugin_path=args.plugin,
        metadata_log_path=args.meta_log,
        metadata_error_log_path=args.meta_error_log,
    )


if __name__ == "__main__":
    main()
