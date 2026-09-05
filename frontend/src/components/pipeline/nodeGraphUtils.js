/**
 * nodeGraphUtils — small shared helpers for a config panel that needs to
 * reason about a node's position in the pipeline graph (not just its own
 * config). Factored out of ColumnMappingConfig.jsx (CM5) so LoadStrategyConfig.jsx
 * (CM7, "use business keys from upstream Column Mapping") can reuse the
 * exact same graph-walk instead of a second, possibly-diverging copy.
 */

/** Walks `edges` upstream/downstream from `nodeId` looking for the
 * nearest node of `type` — the same kind of "resolve the pipeline's real
 * source/target" reasoning execution/pipeline_job_executor.py's
 * _run_target already does at execution time (falling back to the
 * primary Source Dataset connection). Capped at a few hops so an unusual
 * graph shape can't loop forever. */
export function findNearestNodeOfType(nodeId, type, edges, nodesById, direction, maxHops = 6) {
  let frontier = [nodeId];
  const seen = new Set(frontier);
  for (let hop = 0; hop < maxHops && frontier.length; hop++) {
    const next = [];
    for (const id of frontier) {
      const neighborIds = edges
        .filter((e) => (direction === 'upstream' ? e.targetId === id : e.sourceId === id))
        .map((e) => (direction === 'upstream' ? e.sourceId : e.targetId));
      for (const nid of neighborIds) {
        if (seen.has(nid)) continue;
        seen.add(nid);
        const node = nodesById[nid];
        if (!node) continue;
        if (node.type === type) return node;
        next.push(nid);
      }
    }
    frontier = next;
  }
  return null;
}
