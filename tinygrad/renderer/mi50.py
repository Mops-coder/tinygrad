from tinygrad.helpers import Target
from tinygrad.renderer.cstyle import HIPRenderer, HIPCCRenderer
from tinygrad.renderer.llvmir import AMDLLVMRenderer


class _MI50Renderer:
  # Class identity survives Renderer.__reduce__ and separates to_program's cache.
  mi50_tuning = True
  runtime_device = "1002:66a1"

  def __init__(self, target:Target):
    if target.arch.split(":", 1)[0] != "gfx906":
      raise RuntimeError(f"MI50 profile requires gfx906, got {target.arch!r}")
    super().__init__(target)
    self.suffix = f"mi50_{type(self).__name__}"


class MI50HIPRenderer(_MI50Renderer, HIPRenderer):
  base_renderer = HIPRenderer


class MI50LLVMRenderer(_MI50Renderer, AMDLLVMRenderer):
  base_renderer = AMDLLVMRenderer


class MI50HIPCCRenderer(_MI50Renderer, HIPCCRenderer):
  base_renderer = HIPCCRenderer
