from __future__ import annotations

from datetime import datetime

from app.services.metadata_service import MetadataService


def test_guess_capture_time_from_14_digit_pattern() -> None:
    service = MetadataService()
    parsed = service.guess_capture_time_from_filename("IMG_20240102123456.jpg")
    assert parsed == datetime(2024, 1, 2, 12, 34, 56)


def test_guess_capture_time_from_split_date_time_pattern() -> None:
    service = MetadataService()
    parsed = service.guess_capture_time_from_filename("vacation_20230704_090011.jpeg")
    assert parsed == datetime(2023, 7, 4, 9, 0, 11)


def test_guess_capture_time_returns_none_for_unmatched_name() -> None:
    service = MetadataService()
    assert service.guess_capture_time_from_filename("photo_without_timestamp.png") is None
