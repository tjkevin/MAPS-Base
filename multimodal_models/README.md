# 多模态模型（CLIP + Mamba）模块

本文件夹在 **不修改原有 MMDAPS 系统代码** 的前提下，增加了一个独立的多模态识别 / 处理 / 检索模块，主要面向 **视频（图像 + 文本）** 场景：

- 使用 **CLIP（OpenAI）** 提取图像 / 视频帧和文本特征；
- 使用 **Mamba 风格序列块（状态空间模型，PyTorch 版本）** 对视频帧序列做 **长程时序建模**；
- 在 **特征级融合空间** 实现通用多模态任务（图文 / 视频检索等）。

## 目录结构

- `config.py`：多模态配置（CLIP 模型名、Mamba 维度、视频采样参数等）。
- `clip_extractor.py`：基于 Hugging Face Transformers 的 CLIP 特征提取工具：
  - 图像 / 帧特征：`encode_images` / `encode_video_frames`
  - 文本特征：`encode_text`
  - 视频采样：`video_to_frames`（基于 OpenCV）
- `mamba_block.py`：Mamba 风格序列块：
  - `MambaBlockPyTorch`：纯 PyTorch 实现，适用于 Windows / CPU 环境；
  - `get_mamba_block`：在 Linux/CUDA + 安装 `mamba-ssm` 时可选择真·Mamba Block。
- `fusion_model.py`：
  - `CLIPMambaFusion`：CLIP 特征 + Mamba 序列建模，输出统一视频向量；
  - `MultimodalPipeline`：端到端管线（加载 CLIP、编码视频 / 文本、计算相似度）。
- `retrieval.py`：
  - 视频库构建与检索：`build_index` / `search` / `run_retrieval_demo`。
- `__init__.py`：模块导出。
- `requirements_mm.txt`：多模态模块额外依赖（在现有 `requirements.txt` 基础上补充）。

## 安装依赖

在已安装主系统依赖的前提下，在虚拟环境中额外安装多模态相关依赖：

```bash
pip install -r multimodal_models/requirements_mm.txt
```

说明：
- 已复用主系统中的 `torch` 版本；
- 若在 Linux + CUDA 环境，并希望使用官方 Mamba CUDA 内核，可额外安装：

```bash
pip install mamba-ssm causal-conv1d
```

然后在代码中将 `use_native_mamba=True`（参考 `mamba_block.get_mamba_block`）。

## 基本用法示例

### 1. 简单视频检索 Demo

```python
from pathlib import Path
from multimodal_models import MultimodalConfig, run_retrieval_demo

video_dir = Path("videos")  # MMDAPS 自带的视频目录
config = MultimodalConfig(
    clip_model_name="openai/clip-vit-base-patch32",
    video_fps=1.0,
    max_frames=32,
)

results = run_retrieval_demo(
    video_dir=video_dir,
    query="a person speaking",
    top_k=5,
    config=config,
)

for path, score in results:
    print(score, path)
```

### 2. 手动编码视频与文本并计算相似度

```python
from pathlib import Path
import torch

from multimodal_models import MultimodalConfig, MultimodalPipeline

config = MultimodalConfig()
pipe = MultimodalPipeline(config)

# 单个视频向量 (D,)
v_emb = pipe.encode_video(Path("videos/example.mp4"))

# 多个文本向量 (N, D)
texts = ["a person giving a talk", "a street scene", "a static image"]
t_emb = pipe.encode_texts(texts)

sim = pipe.similarity_video_text(v_emb.unsqueeze(0), t_emb).squeeze(0)  # (N,)
print(sim)
```

## 与原系统的关系

- 本模块 **不改动** 现有 `app.py`、`models.py` 等后端业务逻辑；
- 所有新模型与代码都集中在 `multimodal_models/` 下，可单独开发与调试；
- 后续如需在网页中接入多模态检索，只需在现有 Flask 中新增 API，调用本模块提供的接口即可。

## 注意事项

1. 当前 `MambaBlockPyTorch` 为纯 PyTorch 实现，适合在 Windows / CPU 环境快速验证多模态流程。\n   若在 Linux + CUDA 环境并安装了 `mamba-ssm`，可通过 `get_mamba_block(..., use_native=True)` 切换为官方高性能 Mamba Block。\n2. 视频加载依赖 `opencv-python`，如不需要视频支持，只做图文检索，可以不调用 `encode_video_frames`，直接使用 `encode_images` 和 `encode_text`。+
