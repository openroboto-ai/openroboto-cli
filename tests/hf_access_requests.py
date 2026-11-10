"""Construct real SDK responses across the CLI's supported HF SDK range."""

from inspect import signature
from typing import Any

from huggingface_hub.hf_api import AccessRequest


def access_request(**fields: Any) -> AccessRequest:
    if "request_id" in signature(AccessRequest).parameters:
        fields["request_id"] = "fixture-request"
    return AccessRequest(**fields)
