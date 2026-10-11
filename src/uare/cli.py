"""Command line: uare {doctor,fetch-adb,devices,packages,pull,extract,analyze,run,summary,find,
dump-il2cpp,il2cpp-enrich,decompile,fetch-tool,recover-metadata,unpack-native,export-assets,
rebuild,mono-decompile,il2cpp-annotate,generate-meta,recover-shaders,convert-scenes,extract-wwise,
decrypt-hotdll}."""

import argparse
import importlib.util
import json
import logging
import os
import platform as host
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from uare import __version__, logs
from uare.adb import AdbClient, AdbConfig, pull_package, subprocess_runner
from uare.analyze import analyze, load_report, resolve_layout, summarize
from uare.apk import extract_apk_set
from uare.assets import analyze_assets, summarize_assets
from uare.elf import inspect_elf
from uare.errors import ElfFormatError, UareError
from uare.external import run_command
from uare.ghidra import DEFAULT_VENDOR_ROOT as GHIDRA_VENDOR_ROOT
from uare.ghidra import decompile, resolve_ghidra
from uare.ghidra import summarize as summarize_ghidra
from uare.il2cpp_dump import dump_definitions
from uare.il2cpp_tools import DEFAULT_VENDOR_ROOT as DUMPER_VENDOR_ROOT
from uare.il2cpp_tools import enrich, resolve_dumper
from uare.il2cpp_tools import summarize as summarize_enrich
from uare.packers import identify_file
from uare.hotdll import decrypt_directory, summarize as summarize_hotdll
from uare.plan import Prerequisites, asset_modules, build_plan, workdir_state
from uare.plan import summarize as summarize_plan
from uare.tool_fetch import fetch_archive
from uare.outdir import staged_output
from uare.package_tree import package, require_unitypy
from uare.manifest import RunLog, write_manifest
from uare.manifest import summarize as summarize_manifest
from uare.package_tree import summarize as summarize_package
from uare.recover import header_version, recover_file
from uare.platform_tools import (
    DEFAULT_VENDOR_ROOT,
    PLATFORMS,
    PlatformToolsError,
    adb_version,
    fetch_platform,
    host_platform,
    official_url,
    path_lookup,
    resolve_adb,
    ssl_context,
)
from uare.search import format_hits, search_identifiers, search_literals

LOG = logging.getLogger("uare.cli")


def _path_lookup(name: str) -> str | None:
    return path_lookup(name, os.environ.get("PATH", ""))


def _resolved_adb(args: argparse.Namespace) -> str:
    resolved = resolve_adb(args.adb, os.environ, DEFAULT_VENDOR_ROOT, host_platform(), _path_lookup)
    logs.event(LOG, logging.DEBUG, "adb_resolved", path=resolved.path, source=resolved.source)
    return resolved.path


def _adb_client(args: argparse.Namespace) -> AdbClient:
    config = AdbConfig(adb_path=_resolved_adb(args), serial=args.serial, timeout_s=args.timeout,
                       pull_timeout_s=args.pull_timeout, retries=args.retries, backoff_s=1.0)
    return AdbClient(config, subprocess_runner)


def _archives_in(pull_dir: Path) -> list[Path]:
    archives = sorted((pull_dir / "apks").glob("*.apk")) + sorted((pull_dir / "obb").glob("*.obb"))
    if not archives:
        raise UareError(f"no .apk or .obb files under {pull_dir}")
    return archives


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"uare              : {__version__}")
    print(f"python            : {sys.version.split()[0]} ({host.machine()})")
    print(f"host platform     : {host_platform()}")
    resolved = resolve_adb(args.adb, os.environ, DEFAULT_VENDOR_ROOT, host_platform(), _path_lookup)
    print(f"adb               : {resolved.path} [{resolved.source}]")
    print(f"adb version       : {adb_version(resolved.path)}")
    devices = _adb_client(args).devices()
    if not devices:
        print("devices           : none (connect a phone with USB debugging enabled)")
    for device in devices:
        print(f"device            : {device.serial}\t{device.state}")
    return 0


def cmd_fetch_adb(args: argparse.Namespace) -> int:
    platforms = PLATFORMS if args.platform == "all" else (args.platform or host_platform(),)
    if args.sha256 and len(platforms) != 1:
        raise UareError("--sha256 pins one archive; combine it with a single --platform")
    context = ssl_context(os.environ)
    vendor_root = Path(args.vendor_dir)
    for name in platforms:
        record = fetch_platform(name, official_url(name), vendor_root, args.sha256, context)
        print(f"{name}: platform-tools {record.revision} -> {vendor_root / name} (zip sha256 {record.archive_sha256})")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    for device in _adb_client(args).devices():
        print(f"{device.serial}\t{device.state}")
    return 0


def cmd_packages(args: argparse.Namespace) -> int:
    client = _adb_client(args)
    client.select()
    for name in client.third_party_packages():
        if args.filter is None or args.filter.lower() in name.lower():
            print(name)
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    manifest = pull_package(_adb_client(args), args.package, Path(args.out))
    print(json.dumps(asdict(manifest), indent=2))
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.archives]
    if len(inputs) == 1 and inputs[0].is_dir():
        inputs = _archives_in(inputs[0])
    result = extract_apk_set(inputs, Path(args.out))
    print(f"wrote {result.files_written} files ({result.bytes_written} bytes), merged "
          f"{len(result.merged_split_files)} split files, abis {list(result.abis)} -> {result.out_dir}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    report = analyze(Path(args.root), Path(args.out))
    print("\n".join(summarize(asdict(report))))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    require_unitypy()  # checked first: a missing UnityPy must fail before anything is pulled
    workdir = Path(args.workdir)
    pull_dir, extract_dir, analysis_dir = workdir / "pull", workdir / "extracted", workdir / "analysis"
    assets_dir = workdir / "extracted-assets"
    run = RunLog()
    try:
        run.stage("pull", lambda: pull_package(_adb_client(args), args.package, pull_dir))
        run.stage("extract", lambda: extract_apk_set(_archives_in(pull_dir), extract_dir))
        report = run.stage("analyze", lambda: analyze(extract_dir, analysis_dir))
        print("\n".join(summarize(asdict(report))))
        result = run.stage("package", lambda: package(extract_dir, assets_dir))
        print("\n".join(summarize_package(result)))
    finally:
        # Written on success and on failure: a failed run still says which stage stopped it.
        if workdir.is_dir():
            print("\n".join(summarize_manifest(write_manifest(workdir, run))))
    return 0


def _prerequisites(args: argparse.Namespace) -> Prerequisites:
    """Local checks only: adb resolution, importable modules, workdir state. No device is contacted."""
    adb = source = error = None
    try:
        resolved = resolve_adb(args.adb, os.environ, DEFAULT_VENDOR_ROOT, host_platform(), _path_lookup)
        adb, source = resolved.path, resolved.source
    except PlatformToolsError as exc:
        error = str(exc)
    missing = tuple(name for name in asset_modules() if importlib.util.find_spec(name) is None)
    return Prerequisites(adb=adb, adb_source=source, adb_error=error, missing_modules=missing,
                         workdir_state=workdir_state(Path(args.workdir)))


def cmd_plan(args: argparse.Namespace) -> int:
    plan = build_plan(args.package, Path(args.workdir), _prerequisites(args))
    if args.json:
        print(json.dumps(asdict(plan), indent=2, ensure_ascii=False))
    else:
        print("\n".join(summarize_plan(plan)))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    print("\n".join(summarize(load_report(Path(args.analysis_dir)))))
    return 0


def _key_bytes(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise UareError(f"--key must be hex bytes: {exc}") from exc


def _metadata_path(args: argparse.Namespace) -> Path:
    if getattr(args, "metadata", None):
        return Path(args.metadata)
    layout = resolve_layout(Path(args.root))
    return layout.data_dir / "Managed" / "Metadata" / "global-metadata.dat"


def cmd_recover_metadata(args: argparse.Namespace) -> int:
    metadata = _metadata_path(args)
    if not metadata.is_file():
        raise UareError(f"{metadata} not found; this build has no IL2CPP metadata (Mono or non-Unity?)")
    result = recover_file(metadata, key=_key_bytes(args.key), command=args.cmd)
    version = header_version(result.data)
    out_dir = Path(args.out)
    with staged_output(out_dir) as staging:
        (staging / "global-metadata.dat").write_bytes(result.data)
        (staging / "recovery.json").write_text(
            json.dumps({"source": str(metadata), "scheme": result.scheme, "version": version, "bytes": len(result.data)},
                       indent=2), encoding="utf-8")
    print(f"recovered {metadata} via {result.scheme} -> {out_dir / 'global-metadata.dat'} (v{version})")
    return 0


def cmd_dump_il2cpp(args: argparse.Namespace) -> int:
    metadata = _metadata_path(args)
    if not metadata.is_file():
        raise UareError(f"{metadata} not found; this build has no IL2CPP metadata (Mono or non-Unity?)")
    out_dir = Path(args.out)
    with staged_output(out_dir) as staging:
        source = metadata
        if args.decrypt or args.key or args.cmd:
            recovery = recover_file(metadata, key=_key_bytes(args.key), command=args.cmd)
            source = staging / "global-metadata.recovered.dat"
            source.write_bytes(recovery.data)
            print(f"metadata recovered via {recovery.scheme} -> {source.name}")
        result = dump_definitions(source, staging)
    print(f"metadata v{result.version} ({result.layout}) -> {out_dir}")
    for table in result.tables:
        suffix = f" ({table.unreadable} names unreadable)" if table.unreadable else ""
        print(f"  {table.name:<11}: {table.count}{suffix} -> {table.path}")
    grouping = result.grouping
    skipped = f", {grouping.ranges_skipped} ranges skipped" if grouping.ranges_skipped else ""
    print(f"  {'members':<11}: {grouping.member_rows} rows under {grouping.types_grouped}/"
          f"{grouping.types_total} types ({grouping.valid_fraction:.0%} clean{skipped}) -> members.tsv")
    print(result.note)
    return 0


def cmd_export_assets(args: argparse.Namespace) -> int:
    from uare import unitypy_backend

    backend = args.backend
    if backend == "auto":
        backend = "unitypy" if unitypy_backend.available() else "stdlib"
    if backend == "unitypy":
        result = unitypy_backend.export_assets(Path(args.root), Path(args.out))
        print("\n".join(unitypy_backend.summarize(result)))
    else:
        result = analyze_assets(Path(args.root), Path(args.out))
        print("\n".join(summarize_assets(result)))
    return 0


def cmd_unpack_native(args: argparse.Namespace) -> int:
    so = Path(args.so)
    if not so.is_file():
        raise UareError(f"{so} not found")
    match = identify_file(so)
    print(f"packer signature: {match.name} (marker {match.marker!r})" if match
          else "packer signature: none recognised (this does not mean it is unpacked)")
    if not args.cmd:
        print("no --cmd given, so nothing was unpacked. Pass --cmd with a game-specific unpacker "
              "(for example a Frida memory-dump script) that writes the clean .so to {output}.")
        return 0
    data = run_command(args.cmd, so.read_bytes())
    name = Path(args.name).name if args.name else so.name
    out_dir = Path(args.out)
    with staged_output(out_dir) as staging:
        target = staging / name
        target.write_bytes(data)
        try:
            report = inspect_elf(target)
        except ElfFormatError as exc:
            raise UareError(f"unpacker output is not a readable ELF: {exc}") from exc
        (staging / "elf_report.json").write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    packer = f", packer {report.packer}" if report.packer else ""
    print(f"unpacked -> {out_dir / name}: {report.packing_verdict}, exec entropy {report.exec_entropy}{packer}")
    if report.packing_verdict == "likely_packed_or_encrypted":
        print("still packed: the unpacker did not yield a clean .text")
        return 1
    return 0


def cmd_il2cpp_enrich(args: argparse.Namespace) -> int:
    dumper = resolve_dumper(args.dumper, os.environ, DUMPER_VENDOR_ROOT, _path_lookup)
    result = enrich(Path(args.root), Path(args.out), dumper,
                    so=Path(args.so) if args.so else None,
                    metadata=Path(args.metadata) if args.metadata else None, timeout=args.timeout)
    print("\n".join(summarize_enrich(result)))
    return 0


def cmd_decompile(args: argparse.Namespace) -> int:
    ghidra = resolve_ghidra(args.ghidra, os.environ, GHIDRA_VENDOR_ROOT, _path_lookup)
    result = decompile(Path(args.so), Path(args.out), ghidra,
                       decompile_functions=not args.no_decompile, timeout=args.timeout)
    print("\n".join(summarize_ghidra(result)))
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    result = package(Path(args.root), Path(args.out))
    print("\n".join(summarize_package(result)))
    return 0


def cmd_fetch_tool(args: argparse.Namespace) -> int:
    record = fetch_archive(args.url, Path(args.dest), expected_sha256=args.sha256, name=args.name)
    print(f"{record.name}: {len(record.files)} files, archive {record.archive_size} bytes, "
          f"sha256 {record.archive_sha256} -> {args.dest}")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    from uare.rebuild import rebuild
    from uare.rebuild import summarize as summarize_rebuild
    result = rebuild(Path(args.workdir), Path(args.out))
    print("\n".join(summarize_rebuild(result)))
    return 0


def cmd_mono_decompile(args: argparse.Namespace) -> int:
    import shutil as _shutil
    from uare.mono_decompile import decompile_all, resolve_decompiler, DEFAULT_VENDOR_ROOT as ILSPY_VENDOR
    decompiler = resolve_decompiler(args.decompiler, os.environ, ILSPY_VENDOR,
                                    lambda n: _shutil.which(n))
    result = decompile_all(Path(args.managed), Path(args.out), decompiler)
    from uare.mono_decompile import summarize as summarize_mono
    print("\n".join(summarize_mono(result)))
    return 0


def cmd_il2cpp_annotate(args: argparse.Namespace) -> int:
    from uare.il2cpp_merge import merge
    from uare.il2cpp_merge import summarize as summarize_merge
    enrich_dir = Path(args.enrich) if args.enrich else None
    ghidra_dir = Path(args.ghidra_dir) if args.ghidra_dir else None
    result = merge(Path(args.il2cpp_dir), enrich_dir, ghidra_dir, Path(args.out))
    print("\n".join(summarize_merge(result)))
    return 0


def cmd_generate_meta(args: argparse.Namespace) -> int:
    from uare.asset_import import generate_meta_tree
    out = Path(args.out) if args.out else None
    result = generate_meta_tree(Path(args.root), out)
    print(f"processed {result.files_processed} files, {result.folders_processed} folders, "
          f"wrote {result.meta_written} .meta files under {result.root}")
    return 0


def cmd_recover_shaders(args: argparse.Namespace) -> int:
    from uare.shader_recover import recover_shaders
    from uare.shader_recover import summarize as summarize_shaders
    result = recover_shaders(Path(args.source), Path(args.out))
    print("\n".join(summarize_shaders(result)))
    return 0


def cmd_convert_scenes(args: argparse.Namespace) -> int:
    from uare.scene_convert import convert_scenes_and_prefabs
    from uare.scene_convert import summarize as summarize_scenes
    result = convert_scenes_and_prefabs(Path(args.source), Path(args.out))
    print("\n".join(summarize_scenes(result)))
    return 0


def cmd_extract_wwise(args: argparse.Namespace) -> int:
    from uare.wwise import extract_all
    from uare.wwise import summarize as summarize_wwise
    result = extract_all(Path(args.source), Path(args.out))
    print("\n".join(summarize_wwise(result)))
    return 0


def cmd_decrypt_hotdll(args: argparse.Namespace) -> int:
    magic = _bytes_arg(args.magic)
    result = decrypt_directory(
        Path(args.dll_dir), Path(args.out),
        metadata=Path(args.metadata) if args.metadata else None,
        algo=args.algo, key=_bytes_arg(args.key), iv=_bytes_arg(args.iv),
        magic=magic if args.magic is not None else (b"" if args.no_magic else None),
        keys_file=Path(args.keys) if args.keys else None)
    print("\n".join(summarize_hotdll(result)))
    return 0


def _bytes_arg(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise UareError(f"hex argument invalid: {exc}") from exc


def cmd_find(args: argparse.Namespace) -> int:
    search = search_literals if args.literals else search_identifiers
    hits = search(Path(args.analysis_dir), args.pattern, args.context, args.limit)
    if not hits:
        print(f"no match for {args.pattern!r}")
        return 1
    print("\n".join(format_hits(hits)))
    return 0


def _add_adb_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--serial", help="device serial (required when several devices are connected)")
    parser.add_argument("--adb", help="adb binary (default: UARE_ADB, then vendored, then PATH)")
    parser.add_argument("--timeout", type=float, default=120.0, help="timeout for non-transfer adb commands (s)")
    parser.add_argument("--pull-timeout", type=float, default=7200.0, help="timeout for each adb pull (s)")
    parser.add_argument("--retries", type=int, default=2, help="retries for transient adb failures")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uare", description="Unity Android build reverse-engineering toolkit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="show which adb is used, its version, and connected devices")
    _add_adb_options(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("fetch-adb", help="download official platform-tools adb into vendor/")
    p.add_argument("--platform", choices=(*PLATFORMS, "all"), help="default: host platform")
    p.add_argument("--sha256", help="expected sha256 of the downloaded zip (single platform only)")
    p.add_argument("--vendor-dir", default=str(DEFAULT_VENDOR_ROOT), help="default: %(default)s")
    p.set_defaults(func=cmd_fetch_adb)

    p = sub.add_parser("devices", help="list adb devices")
    _add_adb_options(p)
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("packages", help="list third-party packages on the device")
    _add_adb_options(p)
    p.add_argument("--filter", help="case-insensitive substring")
    p.set_defaults(func=cmd_packages)

    p = sub.add_parser("pull", help="pull all APK splits and OBBs of a package")
    _add_adb_options(p)
    p.add_argument("package")
    p.add_argument("--out", required=True, help="empty directory for apks/, obb/, manifest.json")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("extract", help="merge an APK set into one tree (Data/, lib/, assets/, archives/)")
    p.add_argument("archives", nargs="+", help=".apk/.obb files, or a `uare pull` directory")
    p.add_argument("--out", required=True, help="empty output directory")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("analyze", help="report on an extracted tree or a raw Unity Data directory")
    p.add_argument("root")
    p.add_argument("--out", required=True, help="empty directory for report.json and detail files")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("run", help="pull + extract + analyze + full asset export (needs UnityPy) into one work directory")
    _add_adb_options(p)
    p.add_argument("package")
    p.add_argument("--workdir", required=True, help="directory that will hold pull/, extracted/, analysis/")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("plan", help="dry run: list the steps, commands, outputs and missing prerequisites; touches nothing")
    _add_adb_options(p)
    p.add_argument("package")
    p.add_argument("--workdir", required=True, help="the work directory the run would use")
    p.add_argument("--json", action="store_true", help="print the plan as JSON (for agents)")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("summary", help="print the summary of an existing analysis directory")
    p.add_argument("analysis_dir")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("dump-il2cpp", help="categorised IL2CPP name tables (types, methods, fields, ...)")
    p.add_argument("root", help="extracted tree or Unity Data directory")
    p.add_argument("--out", required=True, help="empty directory for the *.tsv name tables")
    p.add_argument("--metadata", help="path to global-metadata.dat (default: under root)")
    p.add_argument("--decrypt", action="store_true", help="recover encrypted/obfuscated metadata before dumping")
    p.add_argument("--key", help="hex XOR key for metadata recovery")
    p.add_argument("--cmd", help="external metadata decryptor ({input}/{output} placeholders, or stdout)")
    p.set_defaults(func=cmd_dump_il2cpp)

    p = sub.add_parser("unpack-native", help="identify a native packer and optionally run an external unpacker")
    p.add_argument("so", help="path to libil2cpp.so (or any native library)")
    p.add_argument("--out", required=True, help="empty directory for the unpacked .so + elf_report.json")
    p.add_argument("--cmd", help="external unpacker ({input}/{output} placeholders, or stdout)")
    p.add_argument("--name", help="output file name (default: input file name)")
    p.set_defaults(func=cmd_unpack_native)

    p = sub.add_parser("il2cpp-enrich", help="run Il2CppDumper/Inspector for RVAs + signatures + dump.cs")
    p.add_argument("root", help="extracted tree (needs lib/<abi>/libil2cpp.so + metadata)")
    p.add_argument("--out", required=True, help="empty directory for methods_rva.tsv, dump.cs, ...")
    p.add_argument("--dumper", help="dumper executable (default: UARE_IL2CPP_DUMPER, vendored, PATH)")
    p.add_argument("--so", help="libil2cpp.so path (default: first under lib/<abi>/)")
    p.add_argument("--metadata", help="global-metadata.dat path (default: under root)")
    p.add_argument("--timeout", type=float, default=1800.0, help="dumper timeout (s)")
    p.set_defaults(func=cmd_il2cpp_enrich)

    p = sub.add_parser("decompile", help="Ghidra headless: function list + C decompilation of a .so")
    p.add_argument("so", help="native library (e.g. an unpacked libil2cpp.so)")
    p.add_argument("--out", required=True, help="empty directory for functions.tsv + decompiled/*.c")
    p.add_argument("--ghidra", help="analyzeHeadless (default: UARE_GHIDRA, vendored, PATH)")
    p.add_argument("--no-decompile", action="store_true", help="list functions only, skip C decompilation")
    p.add_argument("--timeout", type=float, default=7200.0, help="Ghidra timeout (s)")
    p.set_defaults(func=cmd_decompile)

    p = sub.add_parser("package", help="full asset export (needs UnityPy): assets by category and bundle, prefabs, catalog")
    p.add_argument("root", help="extracted tree or Unity Data directory")
    p.add_argument("--out", required=True, help="empty directory for assets/, scripts/, scenes/, catalog.json ...")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("fetch-tool", help="download + unpack an external tool archive into vendor/")
    p.add_argument("--url", required=True, help="archive URL (zip, or a single-file download)")
    p.add_argument("--dest", required=True, help="destination directory, e.g. vendor/il2cpp-dumper")
    p.add_argument("--sha256", help="expected sha256 of the archive")
    p.add_argument("--name", help="label (default: destination name)")
    p.set_defaults(func=cmd_fetch_tool)

    p = sub.add_parser("recover-metadata", help="recover global-metadata.dat from a reversible wrapper")
    p.add_argument("root", help="extracted tree or Unity Data directory")
    p.add_argument("--out", required=True, help="empty directory for global-metadata.dat + recovery.json")
    p.add_argument("--metadata", help="path to global-metadata.dat (default: under root)")
    p.add_argument("--key", help="hex XOR key")
    p.add_argument("--cmd", help="external decryptor ({input}/{output} placeholders, or stdout)")
    p.set_defaults(func=cmd_recover_metadata)

    p = sub.add_parser("export-assets", help="object index + TextAsset bytes + MonoScript table")
    p.add_argument("root", help="extracted tree or Unity Data directory")
    p.add_argument("--out", required=True, help="empty directory for objects.jsonl, textassets/, ...")
    p.add_argument("--backend", choices=("auto", "stdlib", "unitypy"), default="auto",
                   help="stdlib (no deps) or unitypy (bundles + textures/audio/mesh, needs uare[power])")
    p.set_defaults(func=cmd_export_assets)

    p = sub.add_parser("rebuild", help="create a Unity project from a completed uare run workdir")
    p.add_argument("workdir", help="directory from a prior `uare run` (with analysis/, extracted-assets/, ...)")
    p.add_argument("--out", required=True, help="empty directory for the Unity project")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("mono-decompile", help="decompile Mono assemblies to C# via ILSpy")
    p.add_argument("managed", help="path to the Managed/ directory containing .dll files")
    p.add_argument("--out", required=True, help="empty directory for decompiled C# projects")
    p.add_argument("--decompiler", help="ilspycmd binary (default: UARE_ILSPY, vendored, PATH)")
    p.set_defaults(func=cmd_mono_decompile)

    p = sub.add_parser("il2cpp-annotate", help="merge IL2CPP dump + RVA + Ghidra into annotated .cs stubs")
    p.add_argument("il2cpp_dir", help="directory from `uare dump-il2cpp` (types.tsv, members.tsv)")
    p.add_argument("--out", required=True, help="empty directory for annotated .cs stubs")
    p.add_argument("--enrich", help="directory from `uare il2cpp-enrich` (methods_rva.tsv)")
    p.add_argument("--ghidra-dir", help="directory from `uare decompile` (decompiled/*.c)")
    p.set_defaults(func=cmd_il2cpp_annotate)

    p = sub.add_parser("generate-meta", help="generate .meta files for all assets under a directory")
    p.add_argument("root", help="directory containing assets")
    p.add_argument("--out", help="output directory for .meta files (default: alongside originals)")
    p.set_defaults(func=cmd_generate_meta)

    p = sub.add_parser("recover-shaders", help="recover ShaderLab source from type-tree JSON exports")
    p.add_argument("source", help="directory with shader JSON files (from `uare package`)")
    p.add_argument("--out", required=True, help="empty directory for recovered .shader files")
    p.set_defaults(func=cmd_recover_shaders)

    p = sub.add_parser("convert-scenes", help="convert hierarchy JSON to Unity YAML scenes/prefabs")
    p.add_argument("source", help="directory from `uare package` (with scenes/ and/or assets/prefabs/)")
    p.add_argument("--out", required=True, help="empty directory for .unity and .prefab files")
    p.set_defaults(func=cmd_convert_scenes)

    p = sub.add_parser("extract-wwise", help="extract Wwise .bnk sound banks to individual WEM files")
    p.add_argument("source", help="directory containing .bnk files (searched recursively)")
    p.add_argument("--out", required=True, help="empty directory for extracted WEM files")
    p.set_defaults(func=cmd_extract_wwise)

    p = sub.add_parser("decrypt-hotdll",
                       help="decrypt HybridCLR-encrypted hot-update DLLs (keys recovered from "
                            "global-metadata.dat, SHA-256 verified)")
    p.add_argument("dll_dir", help="directory holding the encrypted *.dll files "
                                    "(e.g. <workdir>/extracted-assets/scripts/hotdll)")
    p.add_argument("--out", required=True, help="empty output directory for decrypted/ + keys.json")
    p.add_argument("--metadata", help="global-metadata.dat (default: searched near dll_dir)")
    p.add_argument("--keys", help="keys.json from a previous run (skips the key search)")
    p.add_argument("--algo", choices=("aes-128-cbc", "aes-128-gcm", "chacha20", "rc4", "xor"),
                   help="explicit scheme instead of the search")
    p.add_argument("--key", help="hex key for explicit decryption")
    p.add_argument("--iv", help="hex IV/nonce for explicit decryption")
    p.add_argument("--magic", help="hex magic prefix stripped before the ciphertext")
    p.add_argument("--no-magic", action="store_true", help="the ciphertext starts at byte 0")
    p.set_defaults(func=cmd_decrypt_hotdll)

    p = sub.add_parser("find", help="regex search IL2CPP identifiers (or string literals) of an analysis")
    p.add_argument("analysis_dir")
    p.add_argument("pattern", help="Python regular expression")
    p.add_argument("--literals", action="store_true", help="search string_literals.jsonl instead of identifiers")
    p.add_argument("--context", type=int, default=0, help="rows of context around each match")
    p.add_argument("--limit", type=int, default=50, help="maximum matches")
    p.set_defaults(func=cmd_find)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.configure(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return args.func(args)
    except UareError as exc:
        logs.event(LOG, logging.ERROR, "command_failed", command=args.command, error_type=type(exc).__name__,
                   error=str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
