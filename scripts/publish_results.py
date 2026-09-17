import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


MAX_RELEASE_ASSET_BYTES = 2 * 1024 * 1024 * 1024 - 1

OPTIONAL_ASSETS = {
    "ipv4/result.txt": "ipv4.txt",
    "ipv4/result.m3u": "ipv4.m3u",
    "ipv6/result.txt": "ipv6.txt",
    "ipv6/result.m3u": "ipv6.m3u",
    "epg/epg.xml": "epg.xml",
    "epg/epg.gz": "epg.gz",
}


def _resolve_output_file(path, output_dir):
    candidate = path if path.is_absolute() else Path.cwd() / path
    output_root = output_dir.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"Release input must be inside {output_root}: {path}") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise ValueError(f"Release input must be a regular file: {path}")
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError(f"Release input is empty: {path}")
    if size > MAX_RELEASE_ASSET_BYTES:
        raise ValueError(f"Release input exceeds the GitHub release asset limit: {path}")
    return resolved


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_release_assets(
        final_file,
        destination,
        output_dir="output",
        generated_at=None,
        repository=None,
        source_sha=None,
        workflow_run_id=None,
):
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir = output_dir.resolve(strict=True)

    destination = Path(destination)
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Release destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    final_path = _resolve_output_file(Path(final_file), output_dir)
    if final_path.suffix.lower() != ".txt":
        raise ValueError(f"The configured final_file must use the .txt extension: {final_file}")

    sources = [(final_path, "result.txt")]
    final_m3u = final_path.with_suffix(".m3u")
    if final_m3u.exists():
        sources.append((_resolve_output_file(final_m3u, output_dir), "result.m3u"))
    for relative_path, asset_name in OPTIONAL_ASSETS.items():
        candidate = output_dir / relative_path
        if candidate.exists():
            sources.append((_resolve_output_file(candidate, output_dir), asset_name))

    copied = []
    seen_names = set()
    for source, asset_name in sources:
        if asset_name in seen_names:
            raise ValueError(f"Duplicate release asset name: {asset_name}")
        seen_names.add(asset_name)
        target = destination / asset_name
        shutil.copyfile(source, target)
        copied.append(target)

    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    assets = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(copied, key=lambda item: item.name)
    ]
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at,
        "repository": repository or "",
        "source_sha": source_sha or "",
        "workflow_run_id": workflow_run_id or "",
        "assets": assets,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    checksum_paths = [*copied, manifest_path]
    checksum_lines = [
        f"{_sha256(path)}  {path.name}"
        for path in sorted(checksum_paths, key=lambda item: item.name)
    ]
    (destination / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="Prepare the public IPTV result files for a rolling GitHub release."
    )
    parser.add_argument("--final-file", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()
    prepare_release_assets(
        final_file=args.final_file,
        destination=args.destination,
        output_dir=args.output_dir,
        repository=os.getenv("GITHUB_REPOSITORY"),
        source_sha=os.getenv("GITHUB_SHA"),
        workflow_run_id=os.getenv("GITHUB_RUN_ID"),
    )


if __name__ == "__main__":
    main()
