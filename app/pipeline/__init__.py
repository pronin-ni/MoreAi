"""
Pipeline subsystem — Chain-of-Providers orchestration.

Provides multi-stage pipeline execution for controlled multi-model flows:
  - PipelineDefinition: declarative pipeline spec
  - PipelineStage: individual stage config
  - PipelineContext: execution context with handoff data
  - StageResult: per-stage outcome
  - PipelineTrace: full execution trace
  - PipelineExecutor: sequential stage orchestrator
  - PipelineRegistry: discovery and lifecycle management
"""
