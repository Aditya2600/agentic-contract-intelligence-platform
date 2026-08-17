import { Link } from "@tanstack/react-router";
import type { Run } from "@/api/types";
import { RunStatusBadge, TriggerBadge } from "@/components/doctask/badges";
import { Duration, Money, Timestamp } from "@/components/doctask/primitives";
import { Row, TableFrame, Thead, td, th } from "@/components/doctask/surfaces";

export function RunsTable({ runs, collectionId }: { runs: Run[]; collectionId: string }) {
  return (
    <TableFrame>
      <Thead>
        <th className={th}>Run</th>
        <th className={th}>Trigger</th>
        <th className={th}>Status</th>
        <th className={th}>Started</th>
        <th className={`${th} text-right`}>Duration</th>
        <th className={`${th} text-right`}>Cost</th>
        <th className={`${th} text-right`}>Pending</th>
      </Thead>
      <tbody>
        {runs.map((run) => (
          <Row key={run.id}>
            <td className={td}>
              <Link
                to="/collections/$collectionId/runs/$runId"
                params={{ collectionId, runId: run.id }}
                className="font-mono text-[12px] font-medium text-foreground hover:text-primary hover:underline"
              >
                {run.id}
              </Link>
            </td>
            <td className={td}>
              <TriggerBadge trigger={run.trigger} />
            </td>
            <td className={td}>
              <RunStatusBadge status={run.status} />
            </td>
            <td className={td}>
              <Timestamp value={run.startedAt} />
            </td>
            <td className={`${td} text-right`}>
              <Duration ms={run.durationMs} />
            </td>
            <td className={`${td} text-right`}>
              <Money value={run.costUsd} />
            </td>
            <td className={`${td} num text-right font-mono text-muted-foreground`}>
              {run.pendingReviewCount === 0 ? (
                "—"
              ) : (
                <Link
                  to="/collections/$collectionId/runs/$runId/review"
                  params={{ collectionId, runId: run.id }}
                  className="text-warning-foreground hover:underline"
                >
                  {run.pendingReviewCount}
                </Link>
              )}
            </td>
          </Row>
        ))}
      </tbody>
    </TableFrame>
  );
}
