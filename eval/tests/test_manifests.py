"""The Kubernetes manifests (Assignment 03, Part F): what the api reads matches what the
ingestion Job writes, and every container has resource requests and limits (F5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

K8S = Path(__file__).resolve().parents[2] / "k8s"


def docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def configmap() -> dict[str, str]:
    return next(d for d in docs(K8S / "base" / "configmap.yaml")
                if d["kind"] == "ConfigMap")["data"]


def pinned_collection() -> str:
    dep = docs(K8S / "base" / "api-release.yaml")[0]
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    return next(e["value"] for e in env if e["name"] == "RETRIEVAL_COLLECTION")


def test_the_api_reads_the_collection_the_ingestion_job_writes() -> None:
    data = configmap()
    model = data["EMBED_MODEL"].replace(":", "-").replace("/", "-")
    written = f"rahbar-{model}-c{data['CHUNK_SIZE']}o{data['CHUNK_OVERLAP']}"
    assert pinned_collection() == written


def pod_specs(doc: dict[str, Any]) -> Iterator[dict[str, Any]]:
    kind, spec = doc.get("kind"), doc.get("spec") or {}
    if kind == "Pod":
        yield spec
    elif kind in ("Deployment", "StatefulSet", "Job"):
        yield spec["template"]["spec"]
    elif kind == "CronJob":
        yield spec["jobTemplate"]["spec"]["template"]["spec"]


def manifests() -> list[Path]:
    patches = {"api-release.yaml", "api-traces-patch.yaml"}        # partial objects
    files = [p for p in sorted((K8S / "base").glob("*.yaml")) if p.name not in patches]
    files += sorted(K8S.glob("*.yaml")) + sorted((K8S / "candidate").glob("*.yaml"))
    return files


CONTAINERS = [(p.relative_to(K8S).as_posix(), c["name"], c.get("resources") or {})
              for p in manifests() for d in docs(p) for s in pod_specs(d)
              for c in s.get("initContainers", []) + s["containers"]]


def test_the_manifests_have_containers() -> None:
    names = {(f, n) for f, n, _ in CONTAINERS}
    assert ("base/api.yaml", "api") in names and ("base/chroma-statefulset.yaml", "chroma") in names
    assert ("eval-job.yaml", "eval") in names and ("base/indexer-job.yaml", "indexer") in names


@pytest.mark.parametrize("file,name,resources", CONTAINERS,
                         ids=[f"{f}:{n}" for f, n, _ in CONTAINERS])
def test_every_container_has_requests_and_limits(file: str, name: str,
                                                 resources: dict[str, Any]) -> None:
    for part in ("requests", "limits"):
        assert {"cpu", "memory"} <= set(resources.get(part) or {}), f"{file}: {name} {part}"
