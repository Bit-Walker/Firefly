"""Versioned agent facts, independent of firmware marketing/build versions."""
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class RunningImage(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    algorithm: Literal['sha256-file-v1']
    sha256: str=Field(pattern=r'^[0-9a-f]{64}$')
    size: int=Field(ge=256,le=0x300000)
    project: str=Field(min_length=1,max_length=32)


class AgentRuntime(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    protocol: Literal['edbg.agent/v1']
    family: str=Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,31}$')
    build: str=Field(min_length=1,max_length=31)
    uptime_ms: int=Field(ge=0,le=9007199254740991)
    health: Literal['pending','valid','error']
    image: RunningImage | None
    features: list[Annotated[str,Field(pattern=r'^[a-z][a-z0-9._-]{0,47}/[1-9][0-9]{0,3}$')]]=Field(max_length=32)


def parse(node):
    if node is None:return None
    try:
        telemetry=json.loads(node['telemetry'])
        report=AgentRuntime.model_validate(telemetry.get('runtime'))
        if report.build!=node['version'] or len(set(report.features))!=len(report.features):return None
        return report
    except (ValueError,TypeError,ValidationError):
        return None


def supports(node,feature):
    report=parse(node)
    return bool(report and report.health=='valid' and report.family=='esp32s3' and feature in report.features
                and report.image and report.image.project=='fleet_agent')


def snapshot(node):
    report=parse(node)
    return {'protocol':report.protocol,'family':report.family,'image':report.image.model_dump() if report.image else None,
            'features':sorted(report.features)} if report else None


def matches(node,binding):
    return snapshot(node)==binding.get('agent')
