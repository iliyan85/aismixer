"""Python reference implementation of the synchronous data-plane processor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from secrets import randbelow
import time

from assembler import AIVDMAssembler, AssemblyKey, AssemblyStatus
from core.data_plane import (
    DeduplicationMode,
    OutputBatch,
    ProcessingSnapshot,
    ProcessorOutput,
    ProcessorResetReport,
)
from core.ingress_frame import IngressFrame
from core.metrics import ProcessorMetricsSnapshot
from core.output_builder import build_output_bytes
from core.parsed_sentence import parse_frame_sentences, parse_leading_s_value
from core.s_policy import choose_s_value_from_candidates
from core.state.s_cache import SourceState
from core.target_identity import EgressTargetId
from dedup import Deduplicator


@dataclass(frozen=True, slots=True)
class _ProcessingConfig:
    station_id: str | None
    preserve_ingress_c: bool
    preserve_ingress_gid: bool
    always_tag_single: bool
    gid_digits: int


def _generate_numeric_gid_fixed(digits: int) -> str:
    """Return a cryptographically secure fixed-width numeric group ID."""

    base = 10 ** (digits - 1)
    return str(base + randbelow(9 * base))


class PythonDataPlaneProcessor:
    """Long-lived Python reference processor for one serial runtime consumer.

    One instance exclusively owns its assembler, deduplicator, source state,
    and multipart metadata. Injected mutable components become lifecycle-owned
    by that processor and must not be shared or reset externally. Calls to
    ``process()`` and ``reset()`` are intentionally synchronous and must be
    serialized by the owner; this class adds no locking or worker lifecycle.
    """

    __slots__ = (
        "_config",
        "_assembler",
        "_deduplicator",
        "_wall_clock",
        "_gid_generator",
        "_source_state",
        "_multipart_s_ctx",
        "_multipart_c_ctx",
        "_multipart_gid_ctx",
        "_process_calls",
        "_process_completed",
        "_process_failed",
        "_process_in_flight",
        "_outputless_calls",
        "_output_batches",
        "_output_messages",
        "_reset_calls",
        "_reset_completed",
        "_reset_failed",
        "_reset_in_flight",
    )

    def __init__(
        self,
        *,
        station_id: str | None = "mixstation_1",
        preserve_ingress_c: bool = True,
        preserve_ingress_gid: bool = True,
        always_tag_single: bool = False,
        gid_digits: int = 18,
        assembler: AIVDMAssembler | None = None,
        deduplicator: Deduplicator | None = None,
        wall_clock: Callable[[], float] | None = None,
        gid_generator: Callable[[int], str] | None = None,
        source_state: SourceState | None = None,
    ) -> None:
        self._config = _ProcessingConfig(
            station_id=station_id,
            preserve_ingress_c=preserve_ingress_c,
            preserve_ingress_gid=preserve_ingress_gid,
            always_tag_single=always_tag_single,
            gid_digits=gid_digits,
        )
        self._assembler = (
            AIVDMAssembler()
            if assembler is None
            else assembler
        )
        self._deduplicator = (
            Deduplicator()
            if deduplicator is None
            else deduplicator
        )
        self._wall_clock = time.time if wall_clock is None else wall_clock
        self._gid_generator = (
            _generate_numeric_gid_fixed
            if gid_generator is None
            else gid_generator
        )
        self._source_state = (
            SourceState() if source_state is None else source_state
        )
        self._multipart_s_ctx: dict[AssemblyKey, str] = {}
        self._multipart_c_ctx: dict[AssemblyKey, int] = {}
        self._multipart_gid_ctx: dict[AssemblyKey, frozenset[str]] = {}
        self._process_calls = 0
        self._process_completed = 0
        self._process_failed = 0
        self._process_in_flight = 0
        self._outputless_calls = 0
        self._output_batches = 0
        self._output_messages = 0
        self._reset_calls = 0
        self._reset_completed = 0
        self._reset_failed = 0
        self._reset_in_flight = 0

    def process(
        self,
        frame: IngressFrame,
        snapshot: ProcessingSnapshot,
    ) -> OutputBatch:
        """Process one accepted frame without performing transport I/O."""

        self._process_calls += 1
        self._process_in_flight += 1
        try:
            output_batch = self._process_impl(frame, snapshot)
        except BaseException:
            self._process_failed += 1
            raise
        else:
            self._process_completed += 1
            output_count = len(output_batch.outputs)
            if output_count:
                self._output_batches += 1
                self._output_messages += output_count
            else:
                self._outputless_calls += 1
            return output_batch
        finally:
            self._process_in_flight -= 1

    def _process_impl(
        self,
        frame: IngressFrame,
        snapshot: ProcessingSnapshot,
    ) -> OutputBatch:
        """Run the existing Python processing algorithm."""

        deduplication_mode = snapshot.deduplication_mode
        route_target_ids = snapshot.target_ids

        leading_s = parse_leading_s_value(frame)
        parsed_sentences = parse_frame_sentences(
            frame,
            include_vdo=True,
        )
        outputs: list[ProcessorOutput] = []

        for parsed in parsed_sentences:
            g_value = parsed.tag.g_value
            current_ingress_gid = (
                g_value.preservable_group_id
                if g_value is not None
                else None
            )

            valid_c = (
                parsed.tag.c_value
                if self._config.preserve_ingress_c
                else None
            )
            timestamp_for_header: int | str | None = valid_c

            outcome = self._assembler.feed_parsed_outcome(parsed)

            # Discard old generations before current-arrival metadata can seed
            # a fresh generation with the same assembly key.
            self._discard_multipart_contexts(outcome.discarded_keys)

            if (
                outcome.status in {
                    AssemblyStatus.PENDING,
                    AssemblyStatus.DUPLICATE,
                    AssemblyStatus.COMPLETE,
                }
                and outcome.group_key is not None
                and valid_c is not None
            ):
                previous_c = self._multipart_c_ctx.get(outcome.group_key)
                self._multipart_c_ctx[outcome.group_key] = (
                    valid_c
                    if previous_c is None
                    else min(previous_c, valid_c)
                )

            if (
                outcome.status in {
                    AssemblyStatus.PENDING,
                    AssemblyStatus.DUPLICATE,
                    AssemblyStatus.COMPLETE,
                }
                and outcome.group_key is not None
                and self._config.preserve_ingress_gid
                and current_ingress_gid is not None
            ):
                previous_gids = self._multipart_gid_ctx.get(
                    outcome.group_key,
                    frozenset(),
                )
                self._multipart_gid_ctx[outcome.group_key] = (
                    previous_gids | frozenset((current_ingress_gid,))
                )

            if (
                outcome.status in {
                    AssemblyStatus.PENDING,
                    AssemblyStatus.DUPLICATE,
                }
                and outcome.group_key is not None
                and parsed.tag.s_value is not None
                and g_value is not None
            ):
                self._multipart_s_ctx[outcome.group_key] = parsed.tag.s_value

            if outcome.status in {
                AssemblyStatus.INVALID,
                AssemblyStatus.LIMIT_EXCEEDED,
                AssemblyStatus.PENDING,
                AssemblyStatus.DUPLICATE,
                AssemblyStatus.CONFLICT,
            }:
                continue

            multipart = outcome.sentences

            if (
                outcome.status is AssemblyStatus.COMPLETE
                and outcome.group_key is not None
            ):
                selected_c = (
                    self._multipart_c_ctx.get(outcome.group_key)
                    if self._config.preserve_ingress_c
                    else None
                )
                # Preserve the intentional single/multipart c:0 asymmetry.
                timestamp_for_header = "0" if selected_c == 0 else selected_c

            if (
                outcome.status is AssemblyStatus.COMPLETE
                and outcome.group_key is not None
            ):
                observed_gids = self._multipart_gid_ctx.get(
                    outcome.group_key,
                    frozenset(),
                )
                if (
                    self._config.preserve_ingress_gid
                    and len(observed_gids) == 1
                ):
                    output_gid = next(iter(observed_gids))
                else:
                    output_gid = self._gid_generator(
                        self._config.gid_digits
                    )
            elif (
                self._config.preserve_ingress_gid
                and current_ingress_gid is not None
            ):
                output_gid = current_ingress_gid
            else:
                output_gid = self._gid_generator(self._config.gid_digits)

            total_parts = len(multipart)
            tag_single = total_parts == 1 and self._config.always_tag_single
            logical_key = (
                multipart[0]
                if total_parts == 1
                else tuple(multipart)
            )

            eligible_target_ids: tuple[EgressTargetId, ...] = ()
            if deduplication_mode is DeduplicationMode.GLOBAL:
                eligible_target_ids = route_target_ids
                emit_group = self._deduplicator.is_unique(logical_key)
            elif deduplication_mode is DeduplicationMode.PER_TARGET:
                eligible_target_ids = tuple(
                    target_id
                    for target_id in route_target_ids
                    if self._deduplicator.is_unique(
                        logical_key,
                        scope=target_id,
                    )
                )
                emit_group = bool(eligible_target_ids)
            else:
                raise AssertionError(
                    "Unsupported deduplication mode: "
                    f"{deduplication_mode!r}"
                )

            incoming_s = parsed.tag.s_value
            if (
                outcome.status is AssemblyStatus.COMPLETE
                and outcome.group_key is not None
            ):
                incoming_s = incoming_s or self._multipart_s_ctx.get(
                    outcome.group_key
                )

            for index, full_line in enumerate(
                multipart if emit_group else ()
            ):
                is_first = index == 0
                source_name_or_id = frame.alias_for_s or incoming_s
                s_value = choose_s_value_from_candidates(
                    self._config.station_id,
                    source_name_or_id,
                    leading_s,
                    frame.remote_ip,
                )
                self._source_state.touch_s(s_value)

                if total_parts > 1 or tag_single:
                    g_triplet = (
                        f"{index + 1}-{total_parts}-{output_gid}"
                    )
                else:
                    g_triplet = None

                message = build_output_bytes(
                    full_line,
                    s_value,
                    timestamp_for_header,
                    is_first=is_first,
                    g_triplet=g_triplet,
                    clock=self._wall_clock,
                )
                outputs.append(
                    ProcessorOutput(
                        message=message,
                        target_ids=eligible_target_ids,
                    )
                )

            # Normal completion consumes metadata even when routing or
            # deduplication suppresses every output.
            if (
                outcome.status is AssemblyStatus.COMPLETE
                and outcome.group_key is not None
            ):
                self._discard_multipart_contexts((outcome.group_key,))

        return OutputBatch(outputs=tuple(outputs))

    def reset(self) -> ProcessorResetReport:
        """Reset assembler, deduplicator, source state, then metadata.

        Configuration, injected helpers, and component lifetime statistics
        remain owned by this processor and are preserved.
        """

        self._reset_calls += 1
        self._reset_in_flight += 1
        try:
            report = self._reset_impl()
        except BaseException:
            self._reset_failed += 1
            raise
        else:
            self._reset_completed += 1
            return report
        finally:
            self._reset_in_flight -= 1

    def _reset_impl(self) -> ProcessorResetReport:
        """Run the existing ordered reset implementation."""

        assembler_groups_discarded = len(self._assembler.reset())
        dedup_entries_discarded = self._deduplicator.reset()
        source_entries_discarded = self._source_state.reset()

        multipart_s_contexts_discarded = len(self._multipart_s_ctx)
        self._multipart_s_ctx.clear()
        multipart_c_contexts_discarded = len(self._multipart_c_ctx)
        self._multipart_c_ctx.clear()
        multipart_gid_contexts_discarded = len(self._multipart_gid_ctx)
        self._multipart_gid_ctx.clear()

        return ProcessorResetReport(
            assembler_groups_discarded=assembler_groups_discarded,
            dedup_entries_discarded=dedup_entries_discarded,
            source_entries_discarded=source_entries_discarded,
            multipart_s_contexts_discarded=(
                multipart_s_contexts_discarded
            ),
            multipart_c_contexts_discarded=(
                multipart_c_contexts_discarded
            ),
            multipart_gid_contexts_discarded=(
                multipart_gid_contexts_discarded
            ),
        )

    def metrics_snapshot(self) -> ProcessorMetricsSnapshot:
        """Return fresh immutable lifetime metrics for this processor."""

        return ProcessorMetricsSnapshot(
            process_calls=self._process_calls,
            process_completed=self._process_completed,
            process_failed=self._process_failed,
            process_in_flight=self._process_in_flight,
            outputless_calls=self._outputless_calls,
            output_batches=self._output_batches,
            output_messages=self._output_messages,
            reset_calls=self._reset_calls,
            reset_completed=self._reset_completed,
            reset_failed=self._reset_failed,
            reset_in_flight=self._reset_in_flight,
        )

    def _discard_multipart_contexts(
        self,
        keys: tuple[AssemblyKey, ...],
    ) -> None:
        for key in keys:
            self._multipart_s_ctx.pop(key, None)
            self._multipart_c_ctx.pop(key, None)
            self._multipart_gid_ctx.pop(key, None)
