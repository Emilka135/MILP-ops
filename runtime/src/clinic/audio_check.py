"""One-shot release gate: recognize every current recording before starting the voice service."""
import json
from .admin_rpc import module_call


def main():
    report=module_call('tts','bank.audit',{})
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if report.get('status')!='passed':
        raise SystemExit('Аудиобанк не прошёл проверку. Откройте Аудиофразы → Проверить произношение')


if __name__=='__main__':main()
