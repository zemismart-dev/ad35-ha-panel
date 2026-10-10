#!/usr/bin/env python3
"""Validate and build the manual-install ZIP; HACS installs repository sources."""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
from pathlib import Path
import re
import ssl
import struct
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "ad35_ha_panel"
REPOSITORY = "https://github.com/zemismart-dev/ad35-ha-panel"


def collect() -> tuple[str, dict[str, bytes]]:
    component = ROOT / "custom_components" / DOMAIN
    expected = set(json.loads((ROOT / "scripts/component-files.json").read_text()))
    actual = set()
    for path in component.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink is not permitted: {path.relative_to(ROOT)}")
        if path.is_file() and "__pycache__" not in path.parts:
            actual.add(path.relative_to(component).as_posix())
    if actual != expected:
        raise ValueError(f"Component file set differs: {sorted(actual ^ expected)}")
    siblings = [p.name for p in component.parent.iterdir() if p.is_dir() and p.name != "__pycache__"]
    if siblings != [DOMAIN]:
        raise ValueError("Exactly one integration is allowed")

    contents = {}
    for name in sorted(expected):
        data = (component / name).read_bytes()
        if name.endswith(".py"):
            ast.parse(data, filename=name)
        elif name.endswith(".json"):
            json.loads(data)
        elif name == "brand/icon.png":
            if data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != (256, 256):
                raise ValueError("Expected a 256x256 PNG icon")
        elif name == "commissioning_ca.pem":
            if not re.fullmatch(rb"-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n", data):
                raise ValueError("Expected exactly one public certificate")
            ssl.PEM_cert_to_DER_cert(data.decode("ascii"))
        elif name != "vendor/esp_prov/LICENSE":
            raise ValueError(f"Unreviewed file format: {name}")
        contents[f"custom_components/{DOMAIN}/{name}"] = data

    manifest = json.loads(contents[f"custom_components/{DOMAIN}/manifest.json"])
    version = manifest["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Expected semantic release version")
    if (manifest["domain"] != DOMAIN or not manifest.get("config_flow")
            or manifest["documentation"] != REPOSITORY
            or manifest["issue_tracker"] != REPOSITORY + "/issues"
            or manifest["codeowners"] != ["@zemismart-dev"]):
        raise ValueError("Invalid public integration metadata")
    hacs = json.loads((ROOT / "hacs.json").read_text())
    if not hacs.get("name") or hacs.get("zip_release") or hacs.get("content_in_root"):
        raise ValueError("HACS must install custom_components from repository sources")

    vendor = component / "vendor/esp_prov"
    provenance = json.loads((vendor / "PROVENANCE.json").read_text())
    for record in provenance["files"]:
        if hashlib.sha256((vendor / record["path"]).read_bytes()).hexdigest() != record["local_sha256"]:
            raise ValueError(f"Vendor provenance mismatch: {record['path']}")

    for name in ("README.md", "README.en.md", "CHANGELOG.md", "THIRD_PARTY.md"):
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Invalid documentation file: {name}")
        contents[name] = path.read_bytes()
    # Apply the distribution boundary to documentation as well as executable code.
    for name, data in contents.items():
        if name != f"custom_components/{DOMAIN}/commissioning_ca.pem":
            if re.search(rb"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{43}=(?![A-Za-z0-9+/=])", data):
                raise ValueError(f"Credential-shaped literal in {name}")
        if re.search(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}", data):
            raise ValueError(f"Possible secret in {name}")
    return version, contents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="Write ZIP and SHA256 to .dist/")
    args = parser.parse_args()
    version, contents = collect()
    if not args.build:
        print(f"Validated integration {version}: {len(contents)} distribution files")
        return
    contents["package-info.json"] = (json.dumps({
        "integration": DOMAIN,
        "version": version,
        "repository": REPOSITORY,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())},
    }, ensure_ascii=False, indent=2) + "\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            item = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, data)
    data = buffer.getvalue()
    output_dir = ROOT / ".dist"
    if output_dir.is_symlink():
        raise ValueError("Output directory must not be a symlink")
    output_dir.mkdir(exist_ok=True)
    output = output_dir / f"ad35-ha-panel-integration-{version}.zip"
    digest = hashlib.sha256(data).hexdigest()
    for path, payload in ((output, data), (output.with_suffix(".sha256"), f"{digest}  {output.name}\n".encode())):
        if path.is_symlink() or (path.exists() and path.read_bytes() != payload):
            raise ValueError("Release already exists with different bytes; use a new version")
        if not path.exists():
            with path.open("xb") as file:
                file.write(payload)
    print(json.dumps({"version": version, "file": output.name, "bytes": len(data), "sha256": digest}))


if __name__ == "__main__":
    main()
