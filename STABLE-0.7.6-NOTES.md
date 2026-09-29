# 0.7.6 stable client delivery branch

This branch preserves the customer's currently deployed 0.7.6 client source,
including the fixes completed on 2026-09-29. It is a maintenance snapshot,
not an upgrade or replacement for the repository's 0.10.2 master branch.
The differences against master include older baseline implementations; do not
merge this entire branch into master as a normal forward patch.

## Included fixes

- Distinguish CDP browser targets even when their execution-context IDs match.
- Resolve message ownership from the platform's mall endpoint before page hints.
- Keep sender labels out of buyer nicknames and account routing.
- Recognize matching platform seller-message echoes as send confirmations.
- Select an available sending channel and preserve uncertain-send semantics.
- Keep local session state synchronized with the center.
- Display shop name before buyer name; pin handoff conversations first.
- Count unanswered buyer messages until a confirmed reply; show countdown and
  message time in one shared slot.
- Preserve the configuration editor's Save controls on smaller windows.

## Exit cleanup

The main process invokes the installation's dock cleanup even if the dock was
already running or was replaced by a Wake action. It no longer requires the
dock PID to equal the PID recorded at application startup.

The controller finds its processes by exact executable/script path, instead
of trusting only a PID file. It terminates those controllers and the Edge
processes whose `--user-data-dir` exactly matches this installation's dedicated
profile. It leaves ordinary browser profiles and other installations alone.
The cleanup command reports a nonzero exit status if scoped processes remain.

No customer configuration, machine token, chat history, runtime logs, or
installation binaries are included in this source snapshot.
