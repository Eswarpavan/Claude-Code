import type { TimesFMTag } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

/** Which TimesFM setting the trade's signal was scored with. Untagged (manual/legacy) trades show nothing. */
export function TimesFMBadge({ tag }: { tag: Pick<TimesFMTag, "enabled" | "mode"> | null | undefined }) {
  if (!tag) return null;
  if (!tag.enabled) return <Badge variant="outline" title="Scored without TimesFM">TimesFM off</Badge>;
  return (
    <Badge variant="accent" title={tag.mode === "filter" ? "Kept only because TimesFM's forecast was positive" : "TimesFM forecast was one input to confidence"}>
      TimesFM {tag.mode}
    </Badge>
  );
}
