from __future__ import annotations

import pytest

from ansina.config import ConfigError
from ansina.errors import AnsinaError, ConfigurationError, HeartError, StorageError


def test_ansina_error_has_stable_code() -> None:
    assert AnsinaError.code == "ansina.error"


def test_configuration_error_has_stable_code() -> None:
    assert ConfigurationError.code == "ansina.config.invalid"


def test_details_default_to_empty() -> None:
    error = AnsinaError("something went wrong")
    assert error.details == {}


def test_details_are_preserved() -> None:
    error = AnsinaError("bad request", details={"field": "port"})
    assert error.details == {"field": "port"}


def test_subclass_without_code_is_rejected() -> None:
    with pytest.raises(TypeError, match="must declare its own `code`"):

        class _NoCodeError(AnsinaError):
            pass


def test_config_error_is_an_ansina_error() -> None:
    assert issubclass(ConfigError, AnsinaError)
    assert ConfigError.code == "ansina.config.invalid"


def test_storage_error_has_stable_code() -> None:
    assert issubclass(StorageError, AnsinaError)
    assert StorageError.code == "ansina.storage.error"


def test_heart_error_has_stable_code() -> None:
    assert issubclass(HeartError, AnsinaError)
    assert HeartError.code == "ansina.heart.error"
