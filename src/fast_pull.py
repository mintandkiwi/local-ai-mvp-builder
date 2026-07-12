#!/usr/bin/env python3
"""Download the large Ollama model layer with parallel ranges, then verify it."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


REGISTRY = "https://registry.ollama.ai"


def parse_model(model: str) -> tuple[str, str]:
    if ":" in model:
        repository, tag = model.rsplit(":", 1)
    else:
        repository, tag = model, "latest"
    if not repository or not tag:
        raise ValueError(f"无效模型 ID：{model}")
    component = re.compile(r"^[A-Za-z0-9._-]+$")
    if any(
        not component.fullmatch(part) or part in {".", ".."}
        for part in repository.split("/")
    ):
        raise ValueError(f"无效模型仓库：{repository}")
    if not component.fullmatch(tag) or tag in {".", ".."}:
        raise ValueError(f"无效模型标签：{tag}")
    if "/" not in repository:
        repository = "library/" + repository
    return repository, tag


def fetch_manifest(model: str) -> dict:
    repository, tag = parse_model(model)
    encoded_repo = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
    url = f"{REGISTRY}/v2/{encoded_repo}/manifests/{urllib.parse.quote(tag, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except Exception:
        result = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location", url],
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        )
        return json.loads(result.stdout)


def model_layer(manifest: dict) -> tuple[str, int]:
    candidates = [
        layer
        for layer in manifest.get("layers", [])
        if layer.get("mediaType") == "application/vnd.ollama.image.model"
    ]
    if not candidates:
        raise RuntimeError("manifest 中没有 Ollama model layer")
    layer = max(candidates, key=lambda item: int(item.get("size", 0)))
    digest = str(layer["digest"])
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"不支持的 digest：{digest}")
    return digest, int(layer["size"])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quarantine_path(path: Path) -> Path:
    quarantine = path.with_name(path.name + f".corrupt-{time.time_ns()}")
    os.replace(path, quarantine)
    print(f"quarantined invalid download: {quarantine}")
    return quarantine


def reuse_or_quarantine_blob(final_path: Path, size: int, expected_digest: str) -> bool:
    if not final_path.exists():
        return False
    if final_path.stat().st_size == size and sha256(final_path) == expected_digest:
        return True
    quarantine_path(final_path)
    return False


def completed_temp_is_valid(
    temp_path: Path, size: int, expected_digest: str
) -> bool:
    if not temp_path.exists():
        return False
    control = Path(str(temp_path) + ".aria2")
    if control.exists():
        return False
    if temp_path.stat().st_size == size and sha256(temp_path) == expected_digest:
        return True
    quarantine_path(temp_path)
    control.unlink(missing_ok=True)
    return False


def fast_pull(model: str) -> None:
    if not shutil.which("aria2c"):
        raise RuntimeError("缺少 aria2c；请先运行：brew install aria2")
    manifest = fetch_manifest(model)
    digest, size = model_layer(manifest)
    hex_digest = digest.split(":", 1)[1]
    models_dir = Path(os.environ.get("OLLAMA_MODELS", Path.home() / ".ollama" / "models"))
    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    final_path = blobs_dir / f"sha256-{hex_digest}"
    temp_path = blobs_dir / f"sha256-{hex_digest}.fast-pull"
    repository, _ = parse_model(model)
    blob_url = f"{REGISTRY}/v2/{repository}/blobs/{digest}"

    if reuse_or_quarantine_blob(final_path, size, hex_digest):
        print(f"verified existing model layer: {final_path}")
    else:
        if not completed_temp_is_valid(temp_path, size, hex_digest):
            command = [
                "aria2c",
                "--file-allocation=none",
                "--continue=true",
                "--auto-file-renaming=false",
                "--max-connection-per-server=16",
                "--split=16",
                "--min-split-size=4M",
                "--summary-interval=5",
                f"--dir={blobs_dir}",
                f"--out={temp_path.name}",
                blob_url,
            ]
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
            if proxy:
                command.insert(-1, f"--all-proxy={proxy}")
            subprocess.run(command, check=True)
            if not completed_temp_is_valid(temp_path, size, hex_digest):
                raise RuntimeError("下载完成后的模型层未通过大小或 SHA-256 校验")
        os.replace(temp_path, final_path)
        print(f"verified model layer: {final_path}")

    subprocess.run(["ollama", "pull", model], check=True)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} MODEL", file=sys.stderr)
        return 2
    try:
        fast_pull(sys.argv[1])
    except KeyboardInterrupt:
        print("fast pull cancelled; rerun the same command to resume", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"fast pull failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
