"""Reference-only snapshots shared by the WTI paper desk and the Oil D simulator."""
from dataclasses import asdict, dataclass
import json
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(frozen=True)
class ShadowInputs:
    published_at: float
    index_at: float
    index_price: float
    minute_closes: tuple[float, ...]
    last_shock_at: float | None
    last_roll_at: float | None


def shadow_inputs_path(directory: Path) -> Path:
    return directory / "replay" / "oil-d-reference.json"


def publish_shadow_inputs(directory: Path, inputs: ShadowInputs) -> None:
    path = shadow_inputs_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    file = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(file.name)
    try:
        with file:
            json.dump(asdict(inputs), file, allow_nan=False, separators=(",", ":"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_shadow_inputs(directory: Path) -> ShadowInputs | None:
    try:
        data = json.loads(shadow_inputs_path(directory).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("minute_closes"), list):
            return None
        inputs = ShadowInputs(**{**data, "minute_closes": tuple(data["minute_closes"])})
        timestamps = (inputs.published_at, inputs.index_at, inputs.last_shock_at, inputs.last_roll_at)
        required = (inputs.published_at, inputs.index_at, inputs.index_price, *inputs.minute_closes)
        if (31 <= len(inputs.minute_closes) <= 121 and all(valid_number(v) for v in required)
                and all(value is None or valid_number(value) for value in timestamps)):
            return inputs
    except (OSError, TypeError, ValueError, OverflowError):
        pass
    return None


def valid_number(value):
    return type(value) in (int, float) and isfinite(value) and value > 0
