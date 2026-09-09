"""Resource-bounded clean-room SPICE subset ingest.

The parser reads connectivity and opaque parameter tokens only. It never
evaluates expressions, expands arbitrary directives, or launches a simulator.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from topology_lantern.circuit import (
    CircuitDevice,
    CircuitElementKind,
    CircuitGraph,
    CircuitGraphError,
    CircuitInstance,
    CircuitNet,
    CircuitPort,
    CircuitScope,
    SourceLocation,
    circuit_graph_identity,
    stable_node_id,
)

_ROOT_SCOPE = "__root__"
_IGNORED_DIRECTIVES = {".model", ".option", ".param", ".temp"}
_BIDI_CONTROLS = {"BN", "LRE", "LRI", "LRO", "PDF", "PDI", "RLE", "RLI", "RLO", "FSI"}
_MAX_SOURCE_LABEL_CHARACTERS = 4_096


@dataclass(frozen=True, slots=True)
class IngestLimits:
    """Hard limits applied before graph allocation can grow without bound."""

    max_files: int = 32
    max_file_bytes: int = 524_288
    max_total_bytes: int = 1_048_576
    max_lines: int = 20_000
    max_line_characters: int = 16_384
    max_token_characters: int = 4_096
    max_subcircuits: int = 512
    max_elements: int = 50_000
    max_hierarchy_depth: int = 32


_DEFAULT_LIMITS = IngestLimits()


@dataclass(frozen=True, slots=True)
class _SourceLine:
    source: str
    number: int
    text: str

    @property
    def context(self) -> str:
        return f"{self.source}:{self.number}"


@dataclass(frozen=True, slots=True)
class _RawPrimitive:
    name: str
    kind: CircuitElementKind
    terminals: tuple[tuple[str, str], ...]
    model: str | None
    parameters: tuple[tuple[str, str], ...]
    source: SourceLocation


@dataclass(frozen=True, slots=True)
class _RawInstance:
    name: str
    nets: tuple[str, ...]
    reference: str
    parameters: tuple[tuple[str, str], ...]
    source: SourceLocation


@dataclass(slots=True)
class _RawScope:
    name: str
    ports: tuple[str, ...]
    parameters: tuple[tuple[str, str], ...] = ()
    primitives: list[_RawPrimitive] = field(default_factory=list)
    instances: list[_RawInstance] = field(default_factory=list)
    element_names: set[str] = field(default_factory=set)


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CircuitGraphError(f"{name} must be a positive integer")
    return value


def _validate_limits(limits: IngestLimits) -> None:
    for name in IngestLimits.__dataclass_fields__:
        value = _positive_integer(getattr(limits, name), name)
        if value > getattr(_DEFAULT_LIMITS, name):
            raise CircuitGraphError(
                f"{name} cannot exceed the built-in safety ceiling {getattr(_DEFAULT_LIMITS, name)}"
            )


def _normalize_identifier(value: str, context: str, limits: IngestLimits) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized.strip() != normalized:
        raise CircuitGraphError(f"{context} must be a non-empty trimmed token")
    if len(normalized) > limits.max_token_characters:
        raise CircuitGraphError(f"{context} exceeds the token length limit")
    if any(
        character.isspace()
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        or unicodedata.bidirectional(character) in _BIDI_CONTROLS
        for character in normalized
    ):
        raise CircuitGraphError(f"{context} contains control or whitespace characters")
    if any(character in normalized for character in ("/", "\\", '"', "'", "=")):
        raise CircuitGraphError(f"{context} contains unsupported characters")
    return unicodedata.normalize("NFC", normalized.casefold())


def _logical_lines(text: str, source: str, limits: IngestLimits) -> list[_SourceLine]:
    result: list[_SourceLine] = []
    for number, physical in enumerate(text.splitlines(), 1):
        if len(physical) > limits.max_line_characters:
            raise CircuitGraphError(f"{source}:{number} exceeds the line length limit")
        stripped = physical.strip()
        if not stripped or stripped.startswith("*"):
            continue
        if stripped.startswith("+"):
            if not result:
                raise CircuitGraphError(f"{source}:{number} has an orphan continuation")
            prior = result[-1]
            combined = f"{prior.text} {stripped[1:]}"
            if len(combined) > limits.max_line_characters:
                raise CircuitGraphError(
                    f"{source}:{number} continuation exceeds the logical-line length limit"
                )
            result[-1] = _SourceLine(prior.source, prior.number, combined)
        else:
            result.append(_SourceLine(source, number, stripped))
    return result


def _tokens(line: _SourceLine, limits: IngestLimits) -> tuple[str, ...]:
    tokens = tuple(line.text.split())
    if not tokens or any(len(token) > limits.max_token_characters for token in tokens):
        raise CircuitGraphError(f"{line.context} contains an invalid or oversized token")
    return tokens


class _SafeSourceReader:
    def __init__(self, entry: Path, limits: IngestLimits):
        self.entry = entry.resolve()
        self.root = self.entry.parent
        self.limits = limits
        self.files: list[Path] = []
        self._visited: set[Path] = set()
        self._active: list[Path] = []
        self.total_bytes = 0
        self.total_lines = 0

    def read(self) -> list[_SourceLine]:
        return self._read_file(self.entry)

    def _include_path(self, token: str, parent: Path, context: str) -> Path:
        unquoted = (
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
            else token
        )
        candidate = Path(unquoted)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise CircuitGraphError(f"{context} include path must stay below the entry directory")
        resolved = (parent / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise CircuitGraphError(f"{context} include path escapes the entry directory")
        return resolved

    def _read_file(self, path: Path) -> list[_SourceLine]:
        if path in self._active:
            chain = " -> ".join(item.name for item in (*self._active, path))
            raise CircuitGraphError(f"recursive include: {chain}")
        if path in self._visited:
            return []
        if len(self._visited) >= self.limits.max_files:
            raise CircuitGraphError("SPICE source exceeds the included-file limit")
        remaining = self.limits.max_total_bytes - self.total_bytes
        read_limit = min(self.limits.max_file_bytes, max(remaining, 0))
        try:
            with path.open("rb") as stream:
                payload = stream.read(read_limit + 1)
        except OSError as error:
            raise CircuitGraphError(f"cannot read SPICE source {path}: {error}") from error
        if len(payload) > read_limit:
            if read_limit < self.limits.max_file_bytes:
                raise CircuitGraphError("SPICE source exceeds the total byte limit across files")
            raise CircuitGraphError(f"SPICE file exceeds the per-file byte limit: {path}")
        self.total_bytes += len(payload)
        try:
            text = payload.decode("utf-8")
        except UnicodeError as error:
            raise CircuitGraphError(f"SPICE source is not UTF-8: {path}") from error
        self._visited.add(path)
        self._active.append(path)
        self.files.append(path)
        relative = path.relative_to(self.root).as_posix()
        expanded: list[_SourceLine] = []
        ended = False
        for line in _logical_lines(text, relative, self.limits):
            self.total_lines += 1
            if self.total_lines > self.limits.max_lines:
                raise CircuitGraphError("SPICE source exceeds the logical-line limit")
            tokens = _tokens(line, self.limits)
            directive = tokens[0].casefold()
            if ended:
                raise CircuitGraphError(f"{line.context} contains content after .end")
            if directive == ".end":
                if path != self.entry:
                    raise CircuitGraphError(f"{line.context} included files cannot contain .end")
                if len(tokens) != 1:
                    raise CircuitGraphError(f"{line.context} .end does not accept arguments")
                ended = True
            elif directive == ".include":
                if len(tokens) != 2:
                    raise CircuitGraphError(f"{line.context} .include requires exactly one path")
                included = self._include_path(tokens[1], path.parent, line.context)
                expanded.extend(self._read_file(included))
            else:
                expanded.append(line)
        self._active.pop()
        return expanded


def _parameters(
    tokens: tuple[str, ...], context: str, limits: IngestLimits
) -> tuple[tuple[str, str], ...]:
    values: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise CircuitGraphError(f"{context} parameter must use name=value syntax")
        key, value = token.split("=", 1)
        name = _normalize_identifier(key, f"{context} parameter name", limits)
        if not value or len(value) > limits.max_token_characters:
            raise CircuitGraphError(f"{context} parameter value is empty or oversized")
        if name in values:
            raise CircuitGraphError(f"{context} repeats parameter {name!r}")
        values[name] = value.casefold()
    return tuple(sorted(values.items()))


def _split_positionals_and_parameters(
    tokens: tuple[str, ...], start: int, context: str, limits: IngestLimits
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    markers = [index for index, token in enumerate(tokens) if token.casefold() == "params:"]
    if len(markers) > 1:
        raise CircuitGraphError(f"{context} repeats the PARAMS: marker")
    if markers:
        marker = markers[0]
        if marker < start or any("=" in token for token in tokens[start:marker]):
            raise CircuitGraphError(f"{context} has an out-of-order PARAMS: marker")
        if marker + 1 == len(tokens):
            raise CircuitGraphError(f"{context} PARAMS: marker requires a parameter")
        return tokens[start:marker], _parameters(tokens[marker + 1 :], context, limits)
    parameter_index = next(
        (index for index, token in enumerate(tokens[start:], start) if "=" in token),
        len(tokens),
    )
    return (
        tokens[start:parameter_index],
        _parameters(tokens[parameter_index:], context, limits),
    )


def _primitive(tokens: tuple[str, ...], line: _SourceLine, limits: IngestLimits) -> _RawPrimitive:
    context = line.context
    name = _normalize_identifier(tokens[0], f"{context} element name", limits)
    family = name[0]
    terminal_names: tuple[str, ...]
    kind: CircuitElementKind
    model: str | None = None
    parameter_start: int
    fixed_value: tuple[tuple[str, str], ...] = ()
    if family == "m":
        if len(tokens) < 6:
            raise CircuitGraphError(f"{context} MOSFET requires d g s b and model")
        kind = CircuitElementKind.MOSFET
        terminal_names = ("d", "g", "s", "b")
        model = _normalize_identifier(tokens[5], f"{context} model", limits)
        parameter_start = 6
    elif family in {"r", "c", "i", "v"}:
        if len(tokens) < 4:
            raise CircuitGraphError(f"{context} two-terminal element requires p n and value")
        kind = {
            "r": CircuitElementKind.RESISTOR,
            "c": CircuitElementKind.CAPACITOR,
            "i": CircuitElementKind.CURRENT_SOURCE,
            "v": CircuitElementKind.VOLTAGE_SOURCE,
        }[family]
        terminal_names = ("p", "n")
        fixed_value = (("value", tokens[3].casefold()),)
        parameter_start = 4
    elif family == "d":
        if len(tokens) < 4:
            raise CircuitGraphError(f"{context} diode requires anode cathode and model")
        kind = CircuitElementKind.DIODE
        terminal_names = ("a", "c")
        model = _normalize_identifier(tokens[3], f"{context} model", limits)
        parameter_start = 4
    elif family == "q":
        if len(tokens) < 5:
            raise CircuitGraphError(f"{context} BJT requires c b e and model")
        kind = CircuitElementKind.BJT
        terminal_names = ("c", "b", "e")
        model = _normalize_identifier(tokens[4], f"{context} model", limits)
        parameter_start = 5
    else:
        raise CircuitGraphError(f"{context} unsupported element family {family!r}")
    terminal_tokens = tokens[1 : 1 + len(terminal_names)]
    terminals = tuple(
        (
            terminal,
            _normalize_identifier(net, f"{context} net", limits),
        )
        for terminal, net in zip(terminal_names, terminal_tokens, strict=True)
    )
    parsed_parameters = dict(_parameters(tokens[parameter_start:], context, limits))
    for key, value in fixed_value:
        if key in parsed_parameters:
            raise CircuitGraphError(f"{context} repeats parameter {key!r}")
        parsed_parameters[key] = value
    return _RawPrimitive(
        name,
        kind,
        terminals,
        model,
        tuple(sorted(parsed_parameters.items())),
        SourceLocation(line.source, line.number),
    )


def _instance(tokens: tuple[str, ...], line: _SourceLine, limits: IngestLimits) -> _RawInstance:
    context = line.context
    name = _normalize_identifier(tokens[0], f"{context} instance name", limits)
    positional, parameters = _split_positionals_and_parameters(tokens, 1, context, limits)
    if len(positional) < 2:
        raise CircuitGraphError(f"{context} instance requires nets and a subcircuit reference")
    reference = _normalize_identifier(positional[-1], f"{context} reference", limits)
    nets = tuple(
        _normalize_identifier(token, f"{context} net", limits) for token in positional[:-1]
    )
    return _RawInstance(
        name,
        nets,
        reference,
        parameters,
        SourceLocation(line.source, line.number),
    )


def _parse_lines(
    lines: list[_SourceLine], limits: IngestLimits
) -> tuple[dict[str, _RawScope], frozenset[str]]:
    scopes = {_ROOT_SCOPE: _RawScope(_ROOT_SCOPE, ())}
    global_nets = {"0"}
    current = scopes[_ROOT_SCOPE]
    element_count = 0
    for line in lines:
        tokens = _tokens(line, limits)
        directive = tokens[0].casefold()
        if directive == ".subckt":
            if current.name != _ROOT_SCOPE:
                raise CircuitGraphError(f"{line.context} nested .subckt is not supported")
            if len(tokens) < 3:
                raise CircuitGraphError(f"{line.context} .subckt requires a name and port")
            name = _normalize_identifier(tokens[1], f"{line.context} subcircuit", limits)
            if name == _ROOT_SCOPE or name in scopes:
                raise CircuitGraphError(f"{line.context} duplicate subcircuit {name!r}")
            port_tokens, parameters = _split_positionals_and_parameters(
                tokens, 2, line.context, limits
            )
            ports = tuple(
                _normalize_identifier(token, f"{line.context} port", limits)
                for token in port_tokens
            )
            if not ports or len(ports) != len(set(ports)):
                raise CircuitGraphError(
                    f"{line.context} subcircuit ports must be non-empty and unique"
                )
            if len(scopes) - 1 >= limits.max_subcircuits:
                raise CircuitGraphError("SPICE source exceeds the subcircuit limit")
            current = _RawScope(name, ports, parameters)
            scopes[name] = current
            continue
        if directive == ".ends":
            if current.name == _ROOT_SCOPE:
                raise CircuitGraphError(f"{line.context} .ends has no open subcircuit")
            if len(tokens) > 2:
                raise CircuitGraphError(f"{line.context} .ends accepts at most one name")
            if len(tokens) == 2:
                ended = _normalize_identifier(tokens[1], f"{line.context} subcircuit", limits)
                if ended != current.name:
                    raise CircuitGraphError(f"{line.context} .ends name does not match .subckt")
            current = scopes[_ROOT_SCOPE]
            continue
        if directive == ".global":
            if len(tokens) < 2:
                raise CircuitGraphError(f"{line.context} .global requires at least one net")
            global_nets.update(
                _normalize_identifier(token, f"{line.context} global net", limits)
                for token in tokens[1:]
            )
            continue
        if directive.startswith("."):
            if directive not in _IGNORED_DIRECTIVES:
                raise CircuitGraphError(f"{line.context} unsupported directive {tokens[0]!r}")
            continue
        element_count += 1
        if element_count > limits.max_elements:
            raise CircuitGraphError("SPICE source exceeds the element limit")
        normalized_name = _normalize_identifier(tokens[0], f"{line.context} element", limits)
        if normalized_name in current.element_names:
            raise CircuitGraphError(f"{line.context} duplicate element {normalized_name!r}")
        current.element_names.add(normalized_name)
        if normalized_name.startswith("x"):
            current.instances.append(_instance(tokens, line, limits))
        else:
            current.primitives.append(_primitive(tokens, line, limits))
    if current.name != _ROOT_SCOPE:
        raise CircuitGraphError(f"subcircuit {current.name!r} has no matching .ends")
    if not any(scope.primitives or scope.instances for scope in scopes.values()):
        raise CircuitGraphError("SPICE source contains no circuit elements")
    return scopes, frozenset(global_nets)


def _without_terminal_end(lines: list[_SourceLine], limits: IngestLimits) -> list[_SourceLine]:
    result: list[_SourceLine] = []
    ended = False
    for line in lines:
        if ended:
            raise CircuitGraphError(f"{line.context} contains content after .end")
        tokens = _tokens(line, limits)
        if tokens[0].casefold() == ".end":
            if len(tokens) != 1:
                raise CircuitGraphError(f"{line.context} .end does not accept arguments")
            ended = True
        else:
            result.append(line)
    return result


def _select_top(scopes: dict[str, _RawScope], requested: str | None, limits: IngestLimits) -> str:
    if requested is not None:
        selected = _normalize_identifier(requested, "top subcircuit", limits)
        if selected not in scopes:
            raise CircuitGraphError(f"unknown top subcircuit {selected!r}")
        return selected
    root = scopes[_ROOT_SCOPE]
    if root.primitives or root.instances:
        return _ROOT_SCOPE
    referenced = {instance.reference for scope in scopes.values() for instance in scope.instances}
    candidates = sorted(name for name in scopes if name != _ROOT_SCOPE and name not in referenced)
    if len(candidates) != 1:
        raise CircuitGraphError("top subcircuit is ambiguous; select it explicitly")
    return candidates[0]


def _reference_closure(
    scopes: dict[str, _RawScope], top: str, limits: IngestLimits
) -> tuple[str, ...]:
    visited: set[str] = set()
    active: list[str] = []

    def visit(name: str) -> None:
        if name in active:
            start = active.index(name)
            raise CircuitGraphError(
                "recursive subcircuit reference: " + " -> ".join((*active[start:], name))
            )
        if name in visited:
            return
        if len(active) >= limits.max_hierarchy_depth:
            raise CircuitGraphError("circuit exceeds the hierarchy-depth limit")
        scope = scopes.get(name)
        if scope is None:
            parent = active[-1] if active else top
            raise CircuitGraphError(f"subcircuit {parent!r} references undefined {name!r}")
        active.append(name)
        for instance in sorted(scope.instances, key=lambda item: item.name):
            visit(instance.reference)
        active.pop()
        visited.add(name)

    visit(top)
    return tuple(sorted(visited))


def _build_graph(
    scopes: dict[str, _RawScope],
    closure: tuple[str, ...],
    top: str,
    sources: tuple[str, ...],
    global_nets: frozenset[str],
) -> CircuitGraph:
    built: list[CircuitScope] = []
    for name in closure:
        raw = scopes[name]
        scope_id = stable_node_id("scope", name, name)
        net_names = set(raw.ports)
        for primitive in raw.primitives:
            net_names.update(net for _terminal, net in primitive.terminals)
        for instance in raw.instances:
            net_names.update(instance.nets)
        net_ids = {
            net: stable_node_id("net", "__global__" if net in global_nets else name, net)
            for net in net_names
        }
        ports = tuple(
            CircuitPort(stable_node_id("port", name, port), port, index, net_ids[port])
            for index, port in enumerate(raw.ports)
        )
        nets = tuple(
            CircuitNet(net_ids[net], net, net in raw.ports, net in global_nets)
            for net in sorted(net_names)
        )
        devices = tuple(
            CircuitDevice(
                stable_node_id("device", name, primitive.name),
                primitive.name,
                primitive.kind,
                tuple((terminal, net_ids[net]) for terminal, net in primitive.terminals),
                primitive.model,
                primitive.parameters,
                primitive.source,
            )
            for primitive in sorted(raw.primitives, key=lambda item: item.name)
        )
        instances: list[CircuitInstance] = []
        for instance in sorted(raw.instances, key=lambda item: item.name):
            target = scopes[instance.reference]
            if len(instance.nets) != len(target.ports):
                raise CircuitGraphError(
                    f"instance {name}/{instance.name} passes {len(instance.nets)} nets to "
                    f"{instance.reference}, which declares {len(target.ports)} ports"
                )
            defaults = dict(target.parameters)
            overrides = dict(instance.parameters)
            unknown_parameters = sorted(set(overrides) - set(defaults))
            if unknown_parameters:
                raise CircuitGraphError(
                    f"instance {name}/{instance.name} overrides undeclared parameter "
                    f"{unknown_parameters[0]!r}"
                )
            effective_parameters = tuple(sorted((defaults | overrides).items()))
            instances.append(
                CircuitInstance(
                    stable_node_id("instance", name, instance.name),
                    instance.name,
                    instance.reference,
                    stable_node_id("scope", instance.reference, instance.reference),
                    tuple(
                        (port, net_ids[net])
                        for port, net in zip(target.ports, instance.nets, strict=True)
                    ),
                    instance.parameters,
                    effective_parameters,
                    instance.source,
                )
            )
        built.append(
            CircuitScope(
                scope_id,
                name,
                ports,
                raw.parameters,
                nets,
                devices,
                tuple(instances),
            )
        )
    return CircuitGraph(
        circuit_graph_identity(top, tuple(built)),
        top,
        tuple(built),
        sources,
    )


def parse_spice(
    text: str,
    *,
    top: str | None = None,
    source: str = "<memory>",
    limits: IngestLimits = _DEFAULT_LIMITS,
) -> CircuitGraph:
    """Parse an in-memory SPICE subset; includes require :func:`load_spice`."""
    _validate_limits(limits)
    if not isinstance(source, str) or not source or len(source) > _MAX_SOURCE_LABEL_CHARACTERS:
        raise CircuitGraphError("source label must be a non-empty single-line string")
    source = unicodedata.normalize("NFC", source)
    if (
        not source.strip()
        or len(source) > _MAX_SOURCE_LABEL_CHARACTERS
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROLS
            for character in source
        )
    ):
        raise CircuitGraphError("source label must be a bounded, printable single-line string")
    if not isinstance(text, str):
        raise CircuitGraphError("SPICE source must be text")
    if len(text) > limits.max_file_bytes:
        raise CircuitGraphError("SPICE source exceeds the per-file byte limit")
    if len(text) > limits.max_total_bytes:
        raise CircuitGraphError("SPICE source exceeds the total byte limit")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError as error:
        raise CircuitGraphError("SPICE source is not valid UTF-8 text") from error
    if len(encoded) > limits.max_file_bytes:
        raise CircuitGraphError("SPICE source exceeds the per-file byte limit")
    if len(encoded) > limits.max_total_bytes:
        raise CircuitGraphError("SPICE source exceeds the total byte limit")
    lines = _without_terminal_end(_logical_lines(text, source, limits), limits)
    if len(lines) > limits.max_lines:
        raise CircuitGraphError("SPICE source exceeds the logical-line limit")
    if any(_tokens(line, limits)[0].casefold() == ".include" for line in lines):
        raise CircuitGraphError("in-memory SPICE cannot resolve .include; use load_spice")
    scopes, global_nets = _parse_lines(lines, limits)
    selected = _select_top(scopes, top, limits)
    closure = _reference_closure(scopes, selected, limits)
    return _build_graph(scopes, closure, selected, (source,), global_nets)


def load_spice(
    path: str | Path,
    *,
    top: str | None = None,
    limits: IngestLimits = _DEFAULT_LIMITS,
) -> CircuitGraph:
    """Load a SPICE hierarchy with sandboxed, bounded relative includes."""
    _validate_limits(limits)
    reader = _SafeSourceReader(Path(path), limits)
    lines = reader.read()
    scopes, global_nets = _parse_lines(lines, limits)
    selected = _select_top(scopes, top, limits)
    closure = _reference_closure(scopes, selected, limits)
    sources = tuple(path.relative_to(reader.root).as_posix() for path in reader.files)
    return _build_graph(scopes, closure, selected, sources, global_nets)
