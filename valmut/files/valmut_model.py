from pydantic import BaseModel, Field, ValidationError, ConfigDict
from typing import Optional, Any
from enum import Enum
from typing import Dict, Any, Optional
import re

# --- Utility Function for JSON Patching ---
def create_json_patch(patches: list[Dict[str, Any]]) -> str:
    """
    Creates a base64 encoded JSON Patch string from a list of patch operations.
    Each operation should be a dictionary like:
    {"op": "add", "path": "/metadata/labels", "value": {"my-label": "value"}}
    """
    json_patch_str = json.dumps(patches)
    return base64.b64encode(json_patch_str.encode("utf-8")).decode("utf-8")
    
def to_camel_case(snake_str: str) -> str:
    components = snake_str.split('_')
    return components[0] + ''.join(word.capitalize() for word in components[1:])

class Status(BaseModel):
    """Represents a Kubernetes Status object within AdmissionResponse."""
    code: Optional[int] = None
    message: Optional[str] = None

class PatchType(str, Enum):
    """Defines the type of patch, typically 'JSONPatch'."""
    type: str = "JSONPatch"

class AdmissionRequest(BaseModel):
    """Represents the request sent by Kubernetes to the webhook."""
    model_config = ConfigDict(alias_generator=to_camel_case, populate_by_name=True)
    uid: str
    kind: Dict[str, str]
    resource: Dict[str, str]
    subResource: Optional[str] = None
    requestKind: Optional[Dict[str, str]] = None
    requestResource: Optional[Dict[str, str]] = None
    requestSubResource: Optional[str] = None
    name: Optional[str] = None
    namespace: Optional[str] = None
    operation: str
    userInfo: Dict[str, Any]
    object: Dict[str, Any]
    oldObject: Optional[Dict[str, Any]] = None
    dryRun: Optional[bool] = None
    options: Optional[Dict[str, Any]] = None

class AdmissionResponse(BaseModel):
    """Represents the response sent by the webhook back to Kubernetes."""
    model_config = ConfigDict(alias_generator=to_camel_case, populate_by_name=True)
    uid: str
    allowed: bool
    status: Optional[Status] = None
    patch: Optional[str] = None  # Base64 encoded JSON patch string
    patch_type: Optional[PatchType] =  Field(None)
    warnings: Optional[list[str]] = None

class AdmissionReview(BaseModel):
    """The top-level AdmissionReview object containing the request and response."""
    model_config = ConfigDict(alias_generator=to_camel_case, populate_by_name=True)
    api_version: str = Field("admission.k8s.io/v1", alias="apiVersion")
    #api_version: str = "admission.k8s.io/v1"
    kind: str = "AdmissionReview"
    request: Optional[AdmissionRequest] = None
    response: Optional[AdmissionResponse] = None
