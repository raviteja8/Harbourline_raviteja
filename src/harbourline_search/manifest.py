import json
from collections.abc import Iterator
from dataclasses import dataclass
from os.path import ALLOW_MISSING, realpath
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, ConfigDict, JsonValue, SkipValidation

from .models import DocType


class ManifestRow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    doc_id: str
    revision: int
    title: str
    doc_type: DocType
    groups: SkipValidation[JsonValue]
    path: str | None
    tombstone: bool


@dataclass(frozen=True)
class ManifestDocument:
    row: ManifestRow
    content: bytes | None


def load_manifest(round_dir: Path) -> Iterator[ManifestDocument]:
    round_dir = round_dir.resolve()
    with (round_dir / "manifest.jsonl").open(encoding="utf-8") as manifest:
        for number, line in enumerate(manifest, start=1):
            if number == 1 and line.startswith("\ufeff"):
                raise ValueError("Manifest must not contain a UTF-8 BOM.")
            if not line.strip():
                raise ValueError(f"Blank manifest line {number}.")
            row = ManifestRow.model_validate(json.loads(line))
            document_path = None
            if row.path is not None:
                path = Path(row.path)
                windows_path = PureWindowsPath(row.path)
                if path.is_absolute() or windows_path.drive or windows_path.root:
                    raise ValueError("Manifest document path must be relative.")
                if ".." in path.parts or ".." in windows_path.parts:
                    raise ValueError("Manifest document path must not contain '..' segments.")
                try:
                    document_path = Path(realpath(round_dir / path, strict=ALLOW_MISSING))
                except OSError:
                    raise ValueError(
                        "Manifest document path must stay within the round directory."
                    ) from None
                if not document_path.is_relative_to(round_dir):
                    raise ValueError("Manifest document path must stay within the round directory.")
            if row.tombstone:
                content = None
            else:
                if document_path is None:
                    raise ValueError("Non-tombstone row requires a path.")
                content = document_path.read_bytes()
            yield ManifestDocument(row=row, content=content)
