/**
 * Pipeline <-> Column Mapping attach/upgrade API — wraps
 * /api/pipelines/{id}/mapping/*. See backend/app/api/pipeline_mapping.py.
 */
import apiClient from './apiClient.js';

export const pipelineMappingApi = {
  /** What a given node currently references, read straight from the
   * pipeline's CURRENT PipelineVersion (never from usage history). */
  getNodeMapping: (pipelineId, nodeId) =>
    apiClient.get(`/pipelines/${pipelineId}/mapping/${nodeId}`).then((r) => r.data),

  /** body: {node_id, mapping_id, mapping_version, pipeline_source_*?, pipeline_target_*?}
   * -> {usage_id, consistent, issues}. Registers usage + runs a best-effort
   * source/target consistency check; does NOT itself write the node's
   * config — the caller still does that via the normal canvas Apply/Save. */
  attach: (pipelineId, body) =>
    apiClient.post(`/pipelines/${pipelineId}/mapping/attach`, body).then((r) => r.data),

  /** body: {node_id, to_version} -> {pipeline_id, new_pipeline_version, ...}
   * Creates a brand new PipelineVersion with the node's mappingVersion
   * bumped — history is never mutated. */
  upgrade: (pipelineId, body) =>
    apiClient.post(`/pipelines/${pipelineId}/mapping/upgrade`, body).then((r) => r.data),
};
