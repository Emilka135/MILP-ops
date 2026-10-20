"""Local learned sentence encoder. Hashes/rules are not a production fallback."""
from pathlib import Path
import os
import threading
import numpy as np


class SentenceEncoder:
    name = 'multilingual-e5-small-onnx-int8'
    def __init__(self, folder=None):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        folder=Path(folder or os.environ.get('NLU_MODEL_PATH','modules/nlu/models/multilingual-e5-small'))
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        self.session=ort.InferenceSession(str(folder/'onnx/model_quantized.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
        self.tokenizer=Tokenizer.from_file(str(folder/'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=192);self.tokenizer.enable_padding()
        self.lock=threading.Lock()
    def encode(self,texts):
        batches=[]
        for start in range(0,len(texts),24):
            with self.lock:tokens=self.tokenizer.encode_batch(['query: '+t for t in texts[start:start+24]])
            feed={'input_ids':np.asarray([t.ids for t in tokens],dtype=np.int64),
                  'attention_mask':np.asarray([t.attention_mask for t in tokens],dtype=np.int64),
                  'token_type_ids':np.asarray([t.type_ids for t in tokens],dtype=np.int64)}
            output=self.session.run(None,{i.name:feed[i.name] for i in self.session.get_inputs()})[0]
            mask=feed['attention_mask'][...,None];mean=(output*mask).sum(1)/mask.sum(1).clip(min=1)
            batches.append(mean/np.linalg.norm(mean,axis=1,keepdims=True).clip(min=1e-9))
        return np.concatenate(batches)


CONTROL_EXAMPLES={
 'answer_yes':['Продолжить','Продолжаем','Давайте дальше','Хорошо, продолжайте','Да, данные верны','Да, этот день подходит', 'Да','Конечно, согласен','Этот вариант подходит','Все верно, подтверждаю','Запишите на это время'],
 'answer_no':['Нет','Нет, не согласен','Мне этот вариант не подходит','Нужен другой вариант'],
 'record_opt_out':['Я против записи разговора','Не записывайте наш разговор','Я не согласен на запись голоса'],
 'self_identity':['Вы робот?','Я с человеком разговариваю или с автоматом?'],
 'greeting':['Ало','Здравствуйте','Алло, вы меня слышите?','Добрый день, здравствуйте','Я дозвонилась?'],
 'repeat':['Что?', 'Не расслышал', 'Не поняла вас, повторите пожалуйста','Что вы сказали?','А что от меня требуется?','Погромче, я плохо слышу'],
 'identity_question':['Чьи фамилию имя и отчество назвать?','Чьё ФИО?','Имя отчество врача?','Имя отчество офтальмолога?','Вы спрашиваете как зовут меня или врача?'],
 'already_said':['Я уже сказал вам свои данные','Я уже назвал имя и дату рождения','Да я же уже сказал тебе железка тупая'],
 'back':['Верните меня на предыдущий шаг','Давайте назад','Я хочу исправить предыдущий ответ','Вернуться к предыдущему вопросу'],
 'abuse':['Да иди ты нахер','Пошел ты на хер','Отвали, придурок','Заткнись, идиот','Ты совсем охренел'],
 'operator':['Соедините с сотрудником','Можно с живым человеком поговорить?','Переключите на оператора'],
 'book':['Мне надо записаться к врачу','Можно получить талон на приём?','Мне бы попасть к доктору','Оформите приём у хирурга'],
 'cancel':['Отмените мою запись','Хочу отменить визит','Снимите запись к врачу','Уберите мою запись, я не приду','Отмените талон','Не смогу прийти, снимите меня с приёма'],
 'reschedule':['Перенесите мою запись','Надо перенести визит','Перепишите меня на другую дату','Поменяйте день моего приёма','Можно прийти в другое время вместо назначенного?','Хочу передвинуть запись'],
 'my_booking':['Напомните когда я записана','У меня есть запись к врачу?','Во сколько мне надо прийти?'],
 'confirm':['Подтверждаю что приду на свой приём','Визит в силе, я буду'],
 'info_schedule':['Во сколько закрывается поликлиника?','Какой у вас режим работы?','Когда принимает врач?'],
 'info_address':['Как к вам доехать?','Где расположена поликлиника?'],
 'info_docs':['Какие документы взять с собой?','Нужен ли паспорт на приём?'],
 'medical':['Как лечить мою болезнь?','Какие таблетки принимать?'],
 'provided_data':['Петрова Мария Сергеевна','Иванов Иван Иванович','Сидорова Ольга Петровна','Васильев Иван Иванович','Двенадцатого апреля тысяча девятьсот восемьдесят пятого года','12.04.1985','Один ноль ноль три','Два','Мне удобно утром'],
 'unknown':['Привезите мне две пиццы','Закажите такси к подъезду','Сколько стоит холодильник?'],
}


# Domain intents come from the existing DOCX passports, not invented services.
SOURCE_INTENTS={'С-01':'book','С-02':'book','С-03':'reschedule','С-04':'cancel','С-05':'my_booking',
                'С-06':'home_visit','С-07':'info_schedule','С-08':'results','С-09':'paid',
                'С-11':'waitlist','С-12':'for_other','С-13':'attachment','С-15':'complaint'}
def source_examples():
    import re
    from .scenario_catalog import catalog
    result=[]
    for scenario in catalog()['scenarios']:
        label=SOURCE_INTENTS.get(scenario['id'])
        if label:
            for item in scenario['passport'].get('Триггеры',[]):
                result.extend((label,phrase) for phrase in re.findall('«([^»]+)»',item['text']) if '…' not in phrase)
    return result


class SemanticInterpreter:
    def __init__(self, encoder=None, dictionary=None):
        self.encoder=encoder or SentenceEncoder()
        from .specialty_dictionary import SpecialtyDictionary
        self.dictionary=dictionary or SpecialtyDictionary()
        self.examples=[(label,text) for label,texts in CONTROL_EXAMPLES.items() for text in texts]+source_examples()
        self.matrix=self.encoder.encode([text for _,text in self.examples])
        self.revision=None;self.aliases=[];self.alias_matrix=None
        self.lock=threading.Lock()

    @staticmethod
    def rank(matrix, labels, vector):
        scores=matrix@vector;grouped={}
        for label,score in zip(labels,scores):grouped[label]=max(grouped.get(label,-1),float(score))
        return sorted(grouped.items(),key=lambda x:x[1],reverse=True)

    def interpret(self,text):
        vector=self.encoder.encode([text])[0]
        ranked=self.rank(self.matrix,[label for label,_ in self.examples],vector)
        top,score=ranked[0];margin=score-ranked[1][1]
        intent=top if score>=.84 and margin>=.025 else 'unknown'
        if top=='medical' and score>=.85:intent='medical'
        if top=='abuse' and (score<.90 or margin<.02):intent='unknown'
        if intent=='provided_data':intent='unknown'
        doc=self.dictionary.read()
        with self.lock:
            if doc['revision']!=self.revision:
                self.aliases=[(item['id'],alias) for item in doc['specialties'] for alias in item['aliases']]
                self.alias_matrix=self.encoder.encode(['Хочу записаться к '+alias for _,alias in self.aliases])
                self.revision=doc['revision']
            specialty_rank=self.rank(self.alias_matrix,[k for k,_ in self.aliases],vector)
        resolved=self.dictionary.resolve(text)
        entity_only=False
        if resolved['status']=='resolved':
            from .specialty_dictionary import normalized
            item=next(x for x in doc['specialties'] if x['id']==resolved['id'])
            bare=normalized(text)
            if bare.startswith('к '):bare=bare[2:]
            if bare in {normalized(a) for a in item['aliases']}:intent='book';entity_only=True
        specialty_rank=[r for r in specialty_rank if r[0] not in resolved.get('excluded',[])]
        if resolved['status']=='missing' and specialty_rank and specialty_rank[0][1]>=.85 and (len(specialty_rank)==1 or specialty_rank[0][1]-specialty_rank[1][1]>=.025):
            resolved={'status':'candidate','id':specialty_rank[0][0],'source':'sentence_encoder','score':specialty_rank[0][1]}
        # Entity names do not constitute identity or authorization to write.
        return {'intent':intent,'confidence':score,'margin':margin,'specialty':resolved,
                'entity_only':entity_only,'engine':self.encoder.name,'dictionary_revision':doc['revision'],'alternatives':ranked[:3]}
