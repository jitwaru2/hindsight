"""The operation-hooks extension: our work around the engine's retain, recall and reflect.

Loaded from ``HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_ext_cortana:CortanaOperationHooks``.

- The ``validate_*`` hooks accept every operation unchanged. Pool scoping for documents that
  arrive without a pool moves into ``validate_retain`` (HSIGHT-6).
- ``on_retain_complete`` runs after a retain's writes have committed and before the engine
  enqueues consolidation, once per retain, for the API's synchronous retains and for the
  worker's asynchronous ones (the plugin's session saves and the vault loader both arrive
  asynchronously). Structuring runs here (HSIGHT-4, ``structuring.engine.structure_retain``): the
  retain's new facts become claims, one model call per batch through the engine's retain
  provider, and facts whose call fails are recorded ``structuring-pending`` in the ledger.
  Supersession (HSIGHT-5) follows it here. The engine logs and swallows an exception raised in
  this hook and skips it for a cancelled run, so anything it misses is repaired by reconciliation
  (specification section 6.4).
- ``on_recall_complete`` and ``on_reflect_complete`` write the retrieval log (HSIGHT-7).

The engine calls ``on_startup`` and ``on_shutdown`` only for the tenant and HTTP extensions, not for
this one, so nothing here may depend on them.
"""

import logging

from hindsight_api.extensions import (
    OperationValidatorExtension,
    RecallContext,
    ReflectContext,
    RetainContext,
    RetainResult,
    ValidationResult,
)

from .structuring.engine import structure_retain

logger = logging.getLogger(__name__)


class CortanaOperationHooks(OperationValidatorExtension):
    async def validate_retain(self, ctx: RetainContext) -> ValidationResult:
        return ValidationResult.accept()

    async def validate_recall(self, ctx: RecallContext) -> ValidationResult:
        return ValidationResult.accept()

    async def validate_reflect(self, ctx: ReflectContext) -> ValidationResult:
        return ValidationResult.accept()

    async def on_retain_complete(self, result: RetainResult) -> None:
        logger.info(
            "on_retain_complete bank=%s document=%s items=%d facts=%d success=%s internal=%s",
            result.bank_id,
            result.document_id,
            len(result.unit_ids),
            sum(len(ids) for ids in result.unit_ids),
            result.success,
            result.request_context.internal,
        )
        if not result.success or not any(result.unit_ids):
            return
        await structure_retain(self.context.get_memory_engine(), result)
