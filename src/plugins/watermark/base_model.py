"""
Abstract base class for watermarking models.

All watermarking models must inherit from this class and implement
the `embed` and `detect` methods.
"""
import abc
import inspect
import json
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class BaseModel(abc.ABC):
    """
    Abstract base class for a Watermarking model.

    All watermarking models must implement the `embed` and `detect` methods.
    Each model must have a `config.json` file in its respective directory.
    """

    def __init__(self):
        """
        Initializes the watermarking model by loading its configuration file.

        - Determines the file path of the subclass implementing this base class.
        - Constructs the path to `config.json` in the model's directory.
        - Loads the configuration file, raising an error if it is missing.
        """
        model_file = inspect.getfile(self.__class__)
        model_dir = os.path.dirname(os.path.abspath(model_file))

        self.config_path = os.path.join(model_dir, "config.json")

        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"config.json not found in {self.config_path}")

        with open(self.config_path, "r") as json_file:
            self._config = json.load(json_file)

        self.base_url = None  # subclasses must set this

    def _make_request(
        self, endpoint: str, data: bytes = None, json_data: dict = None, method: str = "POST", timeout: int = 300
    ) -> dict:
        """
        Helper method to make HTTP requests to the model's backend service.

        Args:
            endpoint (str): The specific API endpoint path (e.g., '/embed').
            data (bytes): Raw bytes to send (for audio files). Mutually exclusive with json_data.
            json_data (dict): JSON payload to send. Mutually exclusive with data.
            method (str): The HTTP method (e.g., 'POST', 'GET'). Defaults to 'POST'.
            timeout (int): Request timeout in seconds. Defaults to 300.

        Returns:
            dict: The JSON response from the server.

        Raises:
            ValueError: If self.base_url is not set by the subclass.
            requests.exceptions.RequestException: For connection errors, timeouts, etc.
            requests.exceptions.HTTPError: For bad HTTP responses (4xx, 5xx).
        """
        if not self.base_url:
            raise ValueError(
                f"self.base_url must be set in the __init__ method of {self.__class__.__name__}"
            )

        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"

        try:
            logger.info(f"Making {method} request to {url}")

            if data is not None:
                # Send raw bytes (audio file)
                response = requests.request(method, url, data=data, timeout=timeout)
            elif json_data is not None:
                # Send JSON payload
                response = requests.request(method, url, json=json_data, timeout=timeout)
            else:
                raise ValueError("Either data or json_data must be provided")

            response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Request to {url} failed: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON response from {url}: {e}")
            raise
        except Exception as e:
            logger.error(f"An unexpected error occurred during request to {url}: {e}")
            raise

    @abc.abstractmethod
    def embed(self, audio_bytes: bytes, watermark_data: str) -> bytes:
        """
        Embeds a watermark into the given audio file.

        Args:
            audio_bytes (bytes): The input audio file as bytes.
            watermark_data (str): The watermark data (format depends on implementation).

        Returns:
            bytes: The watermarked audio file as bytes.

        This method must be implemented by subclasses.
        """
        pass

    @abc.abstractmethod
    def detect(self, audio_bytes: bytes) -> Optional[str]:
        """
        Detects (extracts) the watermark from the given audio file.

        Args:
            audio_bytes (bytes): The input audio file containing a possible watermark.

        Returns:
            Optional[str]: The extracted watermark data, or None if no watermark detected.

        This method must be implemented by subclasses.
        """
        pass

    @property
    def name(self) -> str:
        """
        Returns the name of the watermarking model.

        Returns:
            str: The class name of the model instance.
        """
        return self.__class__.__name__

    @property
    def config(self) -> dict:
        """
        Provides read-only access to the model's configuration.

        Returns:
            dict: The model's configuration loaded from `config.json`.
        """
        return self._config
