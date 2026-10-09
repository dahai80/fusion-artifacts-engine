import logging

from fusion_artifacts_engine.visual_primitives.array_grid import ArrayGridCompiler
from fusion_artifacts_engine.visual_primitives.base import BaseCompiler
from fusion_artifacts_engine.visual_primitives.bucket_divider import BucketDividerCompiler
from fusion_artifacts_engine.visual_primitives.data_chart import DataChartCompiler
from fusion_artifacts_engine.visual_primitives.flow_card import FlowCardCompiler
from fusion_artifacts_engine.visual_primitives.geometry_2d import Geometry2DCompiler
from fusion_artifacts_engine.visual_primitives.isometric_3d import Isometric3DCompiler
from fusion_artifacts_engine.visual_primitives.tape_diagram import TapeDiagramCompiler
from fusion_artifacts_engine.visual_primitives.track_timeline import TrackTimelineCompiler

logger = logging.getLogger(__name__)

COMPILER_REGISTRY: dict[str, type[BaseCompiler]] = {
    "array_grid": ArrayGridCompiler,
    "tape_diagram": TapeDiagramCompiler,
    "geometry_2d": Geometry2DCompiler,
    "isometric_3d": Isometric3DCompiler,
    "track_timeline": TrackTimelineCompiler,
    "bucket_divider": BucketDividerCompiler,
    "data_chart": DataChartCompiler,
    "flow_card": FlowCardCompiler,
}

VALID_VISUAL_TYPES = tuple(COMPILER_REGISTRY.keys())


def get_compiler(visual_type: str) -> BaseCompiler:
    cls = COMPILER_REGISTRY.get(visual_type)
    if cls is None:
        logger.warning("unknown visual_type %r, falling back to FlowCard", visual_type)
        return FlowCardCompiler()
    return cls()
