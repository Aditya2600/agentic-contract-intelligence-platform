import { useMemo, useState } from "react";
import type { RegisterItem } from "@/api/types";
import { StateBadge } from "@/components/doctask/badges";
import { Timestamp, ValueDisplay } from "@/components/doctask/primitives";
import { GroupRow, Row, TableFrame, Thead, td, th } from "@/components/doctask/surfaces";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const NO_AGREEMENT = "(no agreement named)";
const ALL = "__all__";
const COLUMNS = 6;

/**
 * Dense obligations table grouped by agreement. Selection is owned by the
 * caller so the evidence inspector stays in sync with the highlighted row.
 */
export function RegisterTable({
  items,
  selectedKey,
  onSelect,
}: {
  items: RegisterItem[];
  selectedKey: string | null;
  onSelect: (item: RegisterItem) => void;
}) {
  const [agreement, setAgreement] = useState(ALL);
  const [state, setState] = useState(ALL);
  const [keyFilter, setKeyFilter] = useState("");

  const agreements = useMemo(
    () => Array.from(new Set(items.map((i) => i.agreementId || NO_AGREEMENT))).sort(),
    [items],
  );

  const filtered = items.filter((i) => {
    const label = i.agreementId || NO_AGREEMENT;
    if (agreement !== ALL && label !== agreement) return false;
    if (state !== ALL && i.state !== state) return false;
    if (keyFilter && !i.key.toLowerCase().includes(keyFilter.toLowerCase())) return false;
    return true;
  });

  const groups = useMemo(() => {
    const map = new Map<string, RegisterItem[]>();
    for (const item of filtered) {
      const label = item.agreementId || NO_AGREEMENT;
      const list = map.get(label);
      if (list) list.push(item);
      else map.set(label, [item]);
    }
    return Array.from(map.entries()).sort(([a], [b]) =>
      a === NO_AGREEMENT ? 1 : b === NO_AGREEMENT ? -1 : a.localeCompare(b),
    );
  }, [filtered]);

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={agreement} onValueChange={setAgreement}>
          <SelectTrigger className="h-7 w-[220px] text-[12px]">
            <SelectValue placeholder="Agreement" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All agreements</SelectItem>
            {agreements.map((a) => (
              <SelectItem key={a} value={a}>
                {a}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select value={state} onValueChange={setState}>
          <SelectTrigger className="h-7 w-[160px] text-[12px]">
            <SelectValue placeholder="State" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All states</SelectItem>
            <SelectItem value="supported">supported</SelectItem>
            <SelectItem value="disputed">disputed</SelectItem>
            <SelectItem value="unsupported">unsupported</SelectItem>
            <SelectItem value="missing">missing</SelectItem>
          </SelectContent>
        </Select>

        <Input
          value={keyFilter}
          onChange={(e) => setKeyFilter(e.target.value)}
          placeholder="Filter obligation key"
          className="h-7 w-[200px] font-mono text-[12px]"
        />

        <div className="ml-auto text-[11.5px] text-muted-foreground">
          {filtered.length} of {items.length} rows
        </div>
      </div>

      <TableFrame>
        <Thead>
          <th className={th}>Obligation</th>
          <th className={th}>Current value</th>
          <th className={th}>State</th>
          <th className={`${th} text-right`}>Evidence</th>
          <th className={`${th} text-right`}>Version</th>
          <th className={th}>Last updated</th>
        </Thead>
        {groups.map(([label, rows]) => (
          <tbody key={label}>
            <GroupRow
              label={label}
              count={rows.length}
              colSpan={COLUMNS}
              muted={label === NO_AGREEMENT}
            />
            {rows.map((row) => (
              <Row
                key={row.registerKey}
                selected={selectedKey === row.registerKey}
                onClick={() => onSelect(row)}
              >
                <td className={`${td} font-mono text-foreground`}>{row.key}</td>
                <td className={td}>
                  <ValueDisplay value={row.value} />
                </td>
                <td className={td}>
                  <StateBadge state={row.state} />
                </td>
                <td className={`${td} num text-right font-mono text-muted-foreground`}>
                  {row.citations.length === 0 ? (
                    <span className="text-danger-foreground">none</span>
                  ) : (
                    `${row.citations.length} quote${row.citations.length > 1 ? "s" : ""}`
                  )}
                </td>
                <td className={`${td} num text-right font-mono text-muted-foreground`}>
                  v{row.version}
                </td>
                <td className={td}>
                  <Timestamp value={row.updatedAt} />
                </td>
              </Row>
            ))}
          </tbody>
        ))}
      </TableFrame>
    </div>
  );
}
