from scripts.check_discord_voice_runtime import check_runtime


def test_check_runtime_passes_when_all_dependencies_present():
    result = check_runtime(
        find_spec=lambda name: object(),
        which=lambda name: f"/usr/bin/{name}",
        find_library=lambda name: f"lib{name}.dylib",
        exists=lambda path: False,
    )

    assert result["ok"] is True
    assert result["missing"] == []


def test_check_runtime_fails_closed_when_ffmpeg_missing():
    result = check_runtime(
        find_spec=lambda name: object(),
        which=lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}",
        find_library=lambda name: f"lib{name}.dylib",
        exists=lambda path: False,
    )

    assert result["ok"] is False
    assert "ffmpeg" in result["missing"]


def test_check_runtime_fails_closed_when_opus_missing():
    result = check_runtime(
        find_spec=lambda name: object(),
        which=lambda name: f"/usr/bin/{name}",
        find_library=lambda name: None if name == "opus" else f"lib{name}.dylib",
        exists=lambda path: False,
    )

    assert result["ok"] is False
    assert "lib:opus" in result["missing"]


def test_check_runtime_accepts_homebrew_opus_fallback():
    result = check_runtime(
        find_spec=lambda name: object(),
        which=lambda name: f"/usr/bin/{name}",
        find_library=lambda name: None if name == "opus" else f"lib{name}.dylib",
        exists=lambda path: path == "/opt/homebrew/lib/libopus.dylib",
    )

    assert result["ok"] is True
    assert "lib:opus" not in result["missing"]


def test_check_runtime_reports_optional_mutagen_without_blocking():
    result = check_runtime(
        find_spec=lambda name: None if name == "mutagen" else object(),
        which=lambda name: f"/usr/bin/{name}",
        find_library=lambda name: f"lib{name}.dylib",
        exists=lambda path: False,
    )

    assert result["ok"] is True
    assert result["missing"] == []
    assert "python:mutagen" in result["optional_missing"]
