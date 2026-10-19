"""Speech sandbox protocol tests; waveform fixture tests validation, not ASR quality."""
import base64
import io
import math
import struct
import wave

import pytest
from clinic.demo_call import DemoCalls


def wav(rate=16000,seconds=.3,silent=False):
    # Explicit unit signal; actual speech/browser acceptance is in debug runner.
    output=io.BytesIO()
    pcm=b''.join(struct.pack('<h',0 if silent else int(1000*math.sin(2*math.pi*440*i/rate))) for i in range(int(rate*seconds)))
    with wave.open(output,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate);f.writeframes(pcm)
    return base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def sandbox():
    calls=[]
    heard=['Хочу записаться к кардиологу.']
    def rpc(module,op,args):
        calls.append((module,op,args))
        if (module,op)==('tts','preview'):return {'pcm_b64':'explicit-unit-fixture','sample_rate':48000}
        if (module,op)==('asr','transcribe'):return {'text':heard[0],'engine':'unit-dependency'}
        raise AssertionError('Sandbox must not call CRM, SMS, NLU, or an arbitrary operation')
    demo=DemoCalls(rpc=rpc)
    def request(op,data=None,client='browser'):
        return demo.request('/api/demo-call/'+op,True,data or {},client=client)
    return demo,request,calls,heard


def test_separate_dtmf_route_never_writes_to_mis(sandbox):
    _,request,calls,_=sandbox
    start=request('start');assert start['audio']['sample_rate']==48000
    key=start['call_id']
    chosen=request('dtmf',{'call_id':key,'digit':'1','op':'book','patient_id':13})
    assert chosen['state']=='demo.confirm' and chosen['selection']['id']=='therapist'
    done=request('dtmf',{'call_id':key,'digit':'1'})
    assert not done['finished'] and not done['executed'] and done['state']=='demo.identity_name'
    assert not sandbox[0].store.tables()['tables']['bookings']
    assert all((module,op)==('tts','preview') for module,op,_ in calls)


def test_real_audio_route_requires_audio_and_uses_asr_before_selection(sandbox):
    _,request,calls,heard=sandbox
    key=request('start')['call_id']
    result=request('audio',{'call_id':key,'wav_b64':wav(),'text':'Хочу к хирургу','module':'crm','operation':'book'})
    assert result['recognized']['text']==heard[0]
    assert result['selection']['id']=='cardiology' and result['state']=='demo.referral'
    assert result['recognized']['source']=='real_asr'
    assert next(args for mod,op,args in calls if mod=='asr')['rate']==16000
    heard[0]='У меня нет направления.'
    assert request('audio',{'call_id':key,'wav_b64':wav()})['state']=='demo.therapist_first'
    assert request('dtmf',{'call_id':key,'digit':'1'})['selection']['id']=='therapist'
    assert all(module in {'asr','tts'} for module,_,_ in calls)


def test_referral_future_or_ambiguous_answer_is_not_claimed_as_present(sandbox):
    _,request,_,heard=sandbox
    key=request('start')['call_id'];request('dtmf',{'call_id':key,'digit':'3'})
    heard[0]='Мне нужно получить направление.'
    assert request('audio',{'call_id':key,'wav_b64':wav()})['state']=='demo.referral'
    heard[0]='Да, у меня есть направление.'
    assert request('audio',{'call_id':key,'wav_b64':wav()})['state']=='demo.confirm'


def test_direct_and_referred_specialists_follow_source_catalog(sandbox):
    _,request,_,heard=sandbox
    for phrase,expected in [('Кардиолог','demo.referral'),('Невролог','demo.referral'),('Хирург','demo.confirm')]:
        key=request('start')['call_id'];request('dtmf',{'call_id':key,'digit':'2'})
        heard[0]=phrase
        assert request('audio',{'call_id':key,'wav_b64':wav()})['state']==expected
        request('end',{'call_id':key})


def test_child_routes_to_pediatrician_and_specialist_buttons_do_not_select_doctors(sandbox):
    _,request,_,heard=sandbox
    key=request('start')['call_id'];heard[0]='Запишите ребёнка к педиатру.'
    assert request('audio',{'call_id':key,'wav_b64':wav()})['selection']['id']=='pediatrician'
    request('end',{'call_id':key});key=request('start')['call_id']
    request('dtmf',{'call_id':key,'digit':'2'})
    assert request('dtmf',{'call_id':key,'digit':'1'})['state']=='demo.specialist'
    assert request('dtmf',{'call_id':key,'digit':'0'})['state']=='demo.operator'


@pytest.mark.parametrize('audio',[None,'not base64',wav(silent=True),wav(rate=44100),wav(seconds=.05)])
def test_invalid_or_silent_audio_does_not_invoke_asr(sandbox,audio):
    _,request,calls,_=sandbox
    key=request('start')['call_id']
    with pytest.raises(ValueError):request('audio',{'call_id':key,'wav_b64':audio})
    assert not any(module=='asr' for module,_,_ in calls)


def test_call_capability_and_client_owner_are_both_required(sandbox):
    _,request,_,_=sandbox
    key=request('start')['call_id']
    with pytest.raises(ValueError):request('dtmf',{'call_id':key,'digit':'1'},client='another-browser')
    with pytest.raises(ValueError):request('dtmf',{'call_id':'guessed','digit':'1'})
    request('end',{'call_id':key})
    with pytest.raises(ValueError):request('dtmf',{'call_id':key,'digit':'1'})


def test_limits_expiry_and_only_fixed_routes(sandbox):
    demo,request,_,_=sandbox
    now=[0];demo.clock=lambda:now[0];demo.ttl=10;demo.limit=1
    key=request('start')['call_id']
    with pytest.raises(ValueError,match='много'):request('start')
    now[0]=11
    with pytest.raises(ValueError):request('dtmf',{'call_id':key,'digit':'1'})
    assert request('start')['call_id']!=key
    with pytest.raises(ValueError):request('book',{'call_id':key})
    with pytest.raises(ValueError):demo.request('/api/demo-call/dtmf',False,{},client='browser')


def test_operator_and_emergency_are_explanations_not_actual_calls(sandbox):
    _,request,calls,heard=sandbox
    key=request('start')['call_id']
    assert request('dtmf',{'call_id':key,'digit':'*'})['state']=='demo.operator'
    request('end',{'call_id':key});key=request('start')['call_id']
    heard[0]='Я не могу дышать.'
    result=request('audio',{'call_id':key,'wav_b64':wav()})
    assert result['state']=='demo.emergency' and result['executed'] is False
    assert all(module in {'tts','asr'} for module,_,_ in calls)


def test_back_clears_selection_and_child_without_referral_keeps_pediatric_route(sandbox):
    _,request,_,heard=sandbox
    key=request('start')['call_id'];heard[0]='Запишите ребёнка к кардиологу.'
    result=request('audio',{'call_id':key,'wav_b64':wav()})
    assert result['selection']['label']=='Детский кардиолог'
    assert request('dtmf',{'call_id':key,'digit':'0'})['state']=='demo.therapist_first'
    assert request('dtmf',{'call_id':key,'digit':'1'})['selection']['id']=='pediatrician'
    result=request('dtmf',{'call_id':key,'digit':'#'})
    assert result['state']=='demo.menu' and result['selection'] is None


def test_reachable_tts_pronunciation_error_is_not_reported_as_outage(monkeypatch):
    import threading
    from calls.services.http import make_server
    from clinic.admin_rpc import module_call

    def reject_pronunciation(operation, data):
        raise ValueError("Не задано проверенное ударение: нейроэнтерокибернетика")

    server = make_server('tts', reject_pronunciation, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('TTS_URL', f'http://127.0.0.1:{server.server_port}')
    try:
        with pytest.raises(ValueError, match="Не задано проверенное ударение"):
            module_call('tts', 'preview', {'text': 'Нейроэнтерокибернетика'})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
