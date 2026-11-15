#!/usr/bin/env python3
"""Build-time smoke test: import the bundle, then slice a cube with the image's presets.

The seeded datadir ships no preset, so the machine/process/filament the CLI is given come
from importing ``--bundle`` through the same ``profiles.import_into`` the app uses.

``--fins-only`` instead exercises the opt-in support-fins path: node + ``app/support_fins.mjs``
run through ``supportfins.fin_model``, and the 3MF they write is read back with
``preview.read_scene`` the way ``GET /api/jobs/{id}/preview`` does. It needs no OrcaSlicer, so
it also runs on a dev host. Use a fixture that actually grows fins (``overhang.stl``);
``cube.stl`` is shallow enough that the CLI legitimately returns zero fins.

The normal mode also generates a two-plate native project and verifies that real Orca
selectors produce the expected distinct layer counts. Each successful check prints a line
starting ``smoke: ok``; failures exit 1 with diagnostics. Runnable as uid 1000.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path


def fail(msg: str, proc: subprocess.CompletedProcess | None = None) -> None:
    print(msg, file=sys.stderr)
    if proc is not None:
        print(f"exit code: {proc.returncode}", file=sys.stderr)
        out = (proc.stdout or "").splitlines()
        err = (proc.stderr or "").splitlines()
        if out:
            print("--- stdout (last 40) ---", file=sys.stderr)
            for line in out[-40:]:
                print(line, file=sys.stderr)
        if err:
            print("--- stderr (last 40) ---", file=sys.stderr)
            for line in err[-40:]:
                print(line, file=sys.stderr)
        print("hint: missing shared library? run: ldd <orca> | grep 'not found'", file=sys.stderr)
        print("hint: offscreen failed? try xvfb-run", file=sys.stderr)
    raise SystemExit(1)


def fins_fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    print("hint: support fins need node on PATH and app/support_fins.mjs beside app/supportfins.py",
          file=sys.stderr)
    raise SystemExit(1)


def check_fins(stl: Path, workdir: Path, app_dir: str) -> None:
    """Run the bundled CLI once and check the finned 3MF the preview dialog would read."""
    if not stl.is_file():
        fins_fail(f"smoke: fins input missing: {stl}")
    sys.path.insert(0, app_dir)
    try:
        import preview
        import supportfins
    except Exception as e:
        fins_fail(f"smoke: cannot import supportfins/preview: {e}")

    reason = supportfins.unavailable_reason()
    if reason:
        fins_fail(f"smoke: support fins unavailable: {reason}")

    outdir = workdir / "fins"
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / f"{stl.stem}-fins.3mf"
    try:
        result = supportfins.fin_model(stl, dest)
    except supportfins.SupportFinsError as e:
        fins_fail(f"smoke: support fins failed: {e}")
    if not dest.is_file() or dest.stat().st_size == 0:
        fins_fail(f"smoke: support fins wrote no 3MF at {dest}")

    try:
        scene = preview.read_scene(dest)
    except preview.PreviewError as e:
        fins_fail(f"smoke: finned 3MF is not readable: {e}")
    supports = [o for o in scene.get("objects", [])
                if o.get("role") == "supports" and o.get("triangles")]
    if not supports:
        fins_fail(f"smoke: no support geometry in {dest.name}: {result.get('stats')}")

    stats = result.get("stats") or {}
    print(f"smoke: ok {dest.name} objects={len(scene['objects'])} supports={len(supports)} "
          f"finTriangles={stats.get('finTriangles')} tines={stats.get('tines')} "
          f"report={result.get('report')!r}")


def _write_box(path: Path, width: float, depth: float, height: float) -> None:
    """Write a closed binary STL; the large footprint forces separate arranged plates."""
    vertices = [(0, 0, 0), (width, 0, 0), (width, depth, 0), (0, depth, 0),
                (0, 0, height), (width, 0, height), (width, depth, height),
                (0, depth, height)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with path.open("wb") as handle:
        handle.write(b"print-portal plate smoke".ljust(80, b"\0"))
        handle.write(struct.pack("<I", len(faces)))
        for face in faces:
            values = [0.0, 0.0, 0.0]
            for index in face:
                values.extend(vertices[index])
            handle.write(struct.pack("<12fH", *values, 0))


def check_plate_selection(orca: str, datadir: Path, machine: Path, process: Path,
                          filament: Path, workdir: Path, env: dict, bed: tuple[float, ...]) -> None:
    """Generate a native project and verify selectors against real geometry, not filenames."""
    from gcode import parse_gcode_stats
    from threemf import inspect_layout

    private = workdir / "multi-plate"
    shutil.rmtree(private, ignore_errors=True)
    private.mkdir(parents=True)
    common = [orca, "--datadir", str(datadir), "--load-settings", f"{machine};{process}",
              "--load-filaments", str(filament)]
    boxes = [private / "short.stl", private / "tall.stl"]
    for path, height in zip(boxes, (2.0, 4.0)):
        _write_box(path, bed[0] * 0.68, bed[1] * 0.68, height)
    project = private / "two-plates.3mf"

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, cwd=private, env=env, capture_output=True, text=True,
                                  timeout=180)
        except subprocess.TimeoutExpired as exc:
            fail(f"smoke: multi-plate timeout after {exc.timeout}s")

    proc = run(common + ["--arrange", "1", "--export-3mf", str(project),
                         *(str(p) for p in boxes)])
    if proc.returncode != 0 or not project.is_file():
        fail("smoke: could not generate the two-plate project", proc)
    try:
        layout = inspect_layout(project, max_uncompressed=64 * 1024 * 1024)
    except Exception as exc:
        fail(f"smoke: generated project failed layout discovery: {exc}", proc)
    if layout.kind != "project" or [plate.index for plate in layout.plates] != [1, 2]:
        fail(f"smoke: expected two discoverable plates, got {layout}", proc)

    for selector, expected_layers in ((1, 10), (2, 20)):
        out = private / f"out-{selector}"
        out.mkdir()
        proc = run(common + ["--slice", str(selector), "--outputdir", str(out), str(project)])
        gcodes = list(out.glob("*.gcode"))
        try:
            result = json.loads((out / "result.json").read_text())
        except (OSError, ValueError) as exc:
            fail(f"smoke: plate {selector} has no valid result.json: {exc}", proc)
        if (proc.returncode != 0 or result.get("return_code") != 0 or len(gcodes) != 1
                or result.get("plate_index") != selector
                or [p.get("id") for p in result.get("sliced_plates", [])] != [selector]):
            fail(f"smoke: plate {selector} produced ambiguous or failed output: {result}", proc)
        stats = parse_gcode_stats(gcodes[0])
        if stats.get("layers") != expected_layers or not (stats.get("grams") or 0) > 0:
            fail(f"smoke: plate {selector} has wrong geometry statistics: {stats}", proc)
        print(f"smoke: ok selector={selector} layers={stats['layers']} {gcodes[0].name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orca", help="orca-slicer binary (slice step only)")
    ap.add_argument("--datadir", help="seed datadir to copy (slice step only)")
    ap.add_argument("--stl", required=True, help="model to slice, or the fins fixture")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--app-dir", default="/app")
    ap.add_argument("--bundle", default="/opt/smoke/bundle.orca_printer")
    ap.add_argument("--bed", default="220x220")
    ap.add_argument("--fins-only", action="store_true",
                    help="only the support-fins step (needs node, not OrcaSlicer)")
    args = ap.parse_args()

    if args.fins_only:
        check_fins(Path(args.stl), Path(args.workdir), args.app_dir)
        return
    if not args.orca or not args.datadir:
        ap.error("--orca and --datadir are required unless --fins-only is given")
    bed = tuple(float(part) for part in args.bed.lower().split("x"))
    orca_path = shutil.which(args.orca)
    if not orca_path:
        fail(f"smoke: OrcaSlicer not found: {args.orca}")
    orca_path = str(Path(orca_path).resolve())

    workdir = Path(args.workdir).resolve()
    datadir = workdir / "datadir"
    outdir = workdir / "out"
    homedir = workdir / "home"
    tmpdir = workdir / "tmp"
    runtime = workdir / "runtime"
    for d in (datadir, outdir, homedir, tmpdir, runtime):
        d.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    # fresh datadir copy
    if datadir.exists():
        shutil.rmtree(datadir)
    shutil.copytree(args.datadir, datadir, dirs_exist_ok=True)

    env = dict(os.environ)
    env["HOME"] = str(homedir)
    env["TMPDIR"] = str(tmpdir)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["DISPLAY"] = ""
    env["LC_ALL"] = "C"

    sys.path.insert(0, args.app_dir)
    try:
        import profiles
    except Exception as e:
        fail(f"smoke: cannot import profiles: {e}")

    bundle = Path(args.bundle)
    try:
        result = profiles.import_into(datadir, bundle.read_bytes(), bundle.name, bed)
    except Exception as e:
        fail(f"smoke: import failed: {e}")

    imported = result["imported"]

    def first_path(ptype: str) -> Path | None:
        for row in imported:
            if row["type"] == ptype:
                return datadir / row["path"]
        return None

    machine_path = first_path("machine")
    process_path = first_path("process")
    filament_path = first_path("filament")
    if not result["ok"] or machine_path is None or process_path is None or filament_path is None:
        print("smoke: import failed", file=sys.stderr)
        for note in result.get("notes", []):
            print(f"note: {note}", file=sys.stderr)
        raise SystemExit(1)

    machine = profiles.effective_preset(machine_path, "machine", datadir)
    profiles.clamp_machine(machine, bed)
    filament = profiles.effective_preset(filament_path, "filament", datadir)
    process = profiles.derived_process_data(process_path, datadir, {})
    eff_dir = workdir / "effective"
    eff_dir.mkdir(parents=True, exist_ok=True)
    eff_machine = eff_dir / "machine.json"
    eff_process = eff_dir / "process.json"
    eff_filament = eff_dir / "filament.json"
    profiles.write_json_atomic(eff_machine, machine)
    profiles.write_json_atomic(eff_process, process)
    profiles.write_json_atomic(eff_filament, filament)

    cmd = [
        orca_path,
        "--datadir", str(datadir),
        "--load-settings", f"{eff_machine};{eff_process}",
        "--load-filaments", str(eff_filament),
        "--slice", "0",
        "--outputdir", str(outdir),
        str(Path(args.stl).resolve()),
    ]

    try:
        proc = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as e:
        print(f"smoke: timeout after {e.timeout}s", file=sys.stderr)
        raise SystemExit(1)

    gcodes = sorted(outdir.glob("*.gcode"))
    if proc.returncode != 0 or not gcodes:
        fail("smoke: slice failed", proc)

    # result.json check
    result_json = outdir / "result.json"
    if result_json.exists():
        try:
            rj = json.loads(result_json.read_text())
            if rj.get("return_code", 0) != 0:
                fail(f"smoke: result.json return_code={rj.get('return_code')}", proc)
        except Exception as e:
            fail(f"smoke: bad result.json: {e}", proc)

    gcode = gcodes[0]
    try:
        from gcode import parse_gcode_stats
    except Exception as e:
        fail(f"smoke: cannot import gcode: {e}")

    stats = parse_gcode_stats(gcode)
    grams = stats.get("grams", 0) or 0
    layers = stats.get("layers", 0) or 0
    time_str = stats.get("time")
    if not (grams and grams > 0):
        fail(f"smoke: grams not > 0: {stats}", proc)
    if not (layers and layers > 0):
        fail(f"smoke: layers not > 0: {stats}", proc)
    if not time_str:
        fail(f"smoke: time empty: {stats}", proc)

    print(f"smoke: ok {gcode.name} {json.dumps(stats)}")
    check_plate_selection(orca_path, datadir, eff_machine, eff_process, eff_filament,
                          workdir, env, bed)


if __name__ == "__main__":
    main()