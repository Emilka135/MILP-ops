"""CUDA mode must reject fallback and demand neural-kernel execution evidence."""
import base64
import json
import sys
from types import SimpleNamespace

import pytest

from calls.asr.onnx_gateway import OnnxAsrModule


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    state = SimpleNamespace(actual=['CUDAExecutionProvider', 'CPUExecutionProvider'],
                            events=[{'args': {'provider': 'CUDAExecutionProvider', 'op_name': 'MatMul'}}],
                            load_options=None, calls=0, preload=False)

    class Session:
        def __init__(self):
            self.fallback_disabled = False
        def get_providers(self):
            return state.actual
        def disable_fallback(self):
            self.fallback_disabled = True
        def end_profiling(self):
            path = tmp_path / 'ort.json'
            path.write_text(json.dumps(state.events))
            return str(path)

    class Adapter:
        def __init__(self):
            self.asr = SimpleNamespace(_encoder=Session())
        def recognize(self, *args, **kwargs):
            state.calls += 1
            return 'Запишите к терапевту.'

    def load_model(*args, **kwargs):
        state.load_options = kwargs
        return Adapter()

    def preload():
        state.preload = True

    monkeypatch.setitem(sys.modules, 'onnx_asr', SimpleNamespace(load_model=load_model))
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(
        SessionOptions=SimpleNamespace, InferenceSession=Session, preload_dlls=preload,
        get_available_providers=lambda: ['CPUExecutionProvider', 'CUDAExecutionProvider'],
        disable_telemetry_events=lambda: None))
    monkeypatch.setenv('ASR_PROVIDER', 'CUDAExecutionProvider')
    monkeypatch.setenv('ASR_PROFILE_DIR', str(tmp_path))
    monkeypatch.delenv('ASR_QUANTIZATION', raising=False)
    return state


def transcribe(module):
    # Explicit unit fixture; real audio acceptance is debug/run_gpu_asr.py.
    return module.handle('transcribe', {'pcm_b64': base64.b64encode(b'\x01\x00' * 160).decode(), 'rate': 16000})


def test_cuda_configuration_and_first_real_execution(runtime):
    module = OnnxAsrModule()
    assert runtime.preload
    options = runtime.load_options
    assert options['quantization'] is None
    assert options['providers'][0][1]['gpu_mem_limit'] == 1024 * 1024 * 1024
    assert options['preprocessor_config']['use_numpy_preprocessors']
    assert module._sessions['encoder'].fallback_disabled
    status, result = transcribe(module)
    assert status == 'ok' and result['cuda_verified']
    assert result['provider'] == 'CUDAExecutionProvider'
    assert module.execution_profiles['encoder']['cuda_compute_kernels'] == 1
    transcribe(module)
    assert runtime.calls == 2


def test_advertised_cuda_does_not_allow_cpu_session(runtime):
    runtime.actual = ['CPUExecutionProvider']
    with pytest.raises(RuntimeError, match='initialized'):
        OnnxAsrModule()


@pytest.mark.parametrize('events', [
    [{'args': {'provider': 'CPUExecutionProvider', 'op_name': 'MatMulInteger'}}],
    [{'args': {'provider': 'CUDAExecutionProvider', 'op_name': 'Gather'}}],
    [{'args': {'provider': 'CUDAExecutionProvider', 'op_name': 'Conv'}},
     {'args': {'provider': 'CPUExecutionProvider', 'op_name': 'MatMulIntegerToFloat'}}],
])
def test_non_gpu_neural_computation_is_a_sticky_failure(runtime, events):
    runtime.events = events
    module = OnnxAsrModule()
    with pytest.raises(RuntimeError, match='CUDA execution verification failed'):
        transcribe(module)
    assert not module.cuda_verified
    with pytest.raises(RuntimeError, match='CUDA execution verification failed'):
        transcribe(module)
    assert runtime.calls == 1


def test_cpu_shape_operators_are_permitted(runtime):
    runtime.events.append({'args': {'provider': 'CPUExecutionProvider', 'op_name': 'Shape'}})
    assert transcribe(OnnxAsrModule())[1]['cuda_verified']
