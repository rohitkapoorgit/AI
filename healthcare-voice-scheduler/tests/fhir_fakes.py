"""Hand-rolled fakes for SandboxSchedulingBackend's two dependencies.

fhirpy has no documented test doubles (confirmed from its own repo — only live-server
integration tests). FakeFhirStore is deliberately partial: it only implements what
src/voice_agent/scheduling/sandbox_backend.py actually calls (`.resources(type).search(**kw)
.fetch_all()`, `.resource(type, **kw).save()`, `.reference(type, id).to_resource()`, and `.save()`
again on the fetched resource to update it). Notably NOT `.patch()` — confirmed live that the real
sandbox server rejects fhirpy's default PATCH content-type, so sandbox_backend.py does a
fetch-mutate-save (PUT) instead; this fake mirrors that, not fhirpy's full surface. If
sandbox_backend.py starts calling something new, this fake needs a matching addition — it will not
silently pass with wrong behavior, it'll just error (AttributeError or similar), which is the
intended failure mode for a deliberately partial fake.

Reference-typed FHIR fields (Schedule.actor, Slot.schedule, Appointment.slot) are normalized into
FakeReference objects with a `.reference` attribute, matching fhirpy's own confirmed behavior of
returning `AsyncFHIRReference` objects for these fields when a resource is fetched from a real
server (verified live against the actual sandbox during implementation).
"""

from fhirpy.base.exceptions import ResourceNotFound

_REFERENCE_LIST_FIELDS = {
    "Schedule": ("actor",),
    "Appointment": ("slot",),
}
_REFERENCE_SINGLE_FIELDS = {
    "Slot": ("schedule",),
}


class FakeReference:
    def __init__(self, reference: str):
        self.reference = reference


def _to_fake_ref(value):
    if isinstance(value, FakeReference):
        return value
    if isinstance(value, dict) and "reference" in value:
        return FakeReference(value["reference"])
    return value


def _normalize_references(resource_type: str, data: dict) -> dict:
    for field in _REFERENCE_LIST_FIELDS.get(resource_type, ()):
        if field in data and data[field] is not None:
            data[field] = [_to_fake_ref(v) for v in data[field]]
    for field in _REFERENCE_SINGLE_FIELDS.get(resource_type, ()):
        if field in data and data[field] is not None:
            data[field] = _to_fake_ref(data[field])
    return data


class FakeResource(dict):
    """A resource that knows how to save itself back to the store it came from -- whether it was
    just constructed via `.resource()` (no id yet, save() assigns one -- create) or fetched via
    `.to_resource()` (already has an id, save() overwrites in place -- update). Matches the real
    fetch-mutate-save pattern sandbox_backend.py uses instead of `.patch()`."""

    def __init__(self, store: "FakeFhirStore", resource_type: str, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._type = resource_type

    @property
    def id(self):
        return self.get("id")

    async def save(self) -> "FakeResource":
        if "id" not in self:
            self["id"] = str(self._store.next_id())
        _normalize_references(self._type, self)
        self._store.data[(self._type, self["id"])] = self
        return self


class FakeSearchSet:
    def __init__(self, resources: list[FakeResource]):
        self._resources = resources
        self._filters: dict = {}

    def search(self, **kwargs) -> "FakeSearchSet":
        self._filters.update(kwargs)
        return self

    def limit(self, _n: int) -> "FakeSearchSet":
        return self

    async def fetch_all(self) -> list[FakeResource]:
        return _apply_filters(self._resources, self._filters)

    async def fetch(self) -> list[FakeResource]:
        return _apply_filters(self._resources, self._filters)


def _apply_filters(resources: list[FakeResource], filters: dict) -> list[FakeResource]:
    matched = []
    for resource in resources:
        if _matches(resource, filters):
            matched.append(resource)
    return matched


def _matches(resource: FakeResource, filters: dict) -> bool:
    for key, value in filters.items():
        if key == "start" and isinstance(value, str) and value.startswith("ge"):
            if resource.get("start", "") < value[2:]:
                return False
        elif key in _REFERENCE_SINGLE_FIELDS.get("Slot", ()):
            ref = resource.get(key)
            if ref is None or ref.reference != value:
                return False
        elif resource.get(key) != value:
            return False
    return True


class FakeReferenceHandle:
    def __init__(self, store: "FakeFhirStore", resource_type: str, resource_id: str):
        self._store = store
        self._type = resource_type
        self._id = resource_id

    async def to_resource(self) -> FakeResource:
        key = (self._type, self._id)
        if key not in self._store.data:
            raise ResourceNotFound(f"{self._type}/{self._id} not found")
        return self._store.data[key]


class FakeFhirStore:
    """Stands in for fhirpy's AsyncFHIRClient. Pass an instance of this directly as
    SandboxSchedulingBackend(fhir_client=...) — it implements .resources()/.resource()/
    .reference() with matching signatures."""

    def __init__(self):
        self.data: dict[tuple[str, str], FakeResource] = {}
        self._id_counter = 1000

    def next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def resources(self, resource_type: str) -> FakeSearchSet:
        of_type = [r for (t, _rid), r in self.data.items() if t == resource_type]
        return FakeSearchSet(of_type)

    def resource(self, resource_type: str, **kwargs) -> FakeResource:
        return FakeResource(self, resource_type, **kwargs)

    def reference(self, resource_type: str, id: str) -> FakeReferenceHandle:
        return FakeReferenceHandle(self, resource_type, id)

    def seed(self, resource_type: str, resource_id: str, **fields) -> FakeResource:
        resource = FakeResource(self, resource_type, id=resource_id, **fields)
        _normalize_references(resource_type, resource)
        self.data[(resource_type, resource_id)] = resource
        return resource


class FakeNppesClient:
    """Stands in for NppesClient. Pass as SandboxSchedulingBackend(nppes_client=...)."""

    def __init__(
        self,
        *,
        search_results: list[dict] | None = None,
        by_number: dict[str, dict] | None = None,
        raise_error: Exception | None = None,
    ):
        self.search_results = search_results if search_results is not None else []
        self.by_number = by_number or {}
        self.raise_error = raise_error
        self.search_calls: list[dict] = []

    async def search(self, **params) -> list[dict]:
        self.search_calls.append(params)
        if self.raise_error:
            raise self.raise_error
        return self.search_results

    async def get_by_number(self, npi: str) -> dict | None:
        return self.by_number.get(npi)

    async def aclose(self) -> None:
        pass
