"""Conservative automatic screening; recognizer agreement does not prove naturalness."""
import re
from .normalize import normalize_text

def tokens(text):
    return re.findall('[а-яё]+',normalize_text(text).lower().replace('ё','е').replace('+',''))


def compare(expected,actual):
    a,b=tokens(expected),tokens(actual);prev=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        row=[i]
        for j,y in enumerate(b,1):row.append(min(row[-1]+1,prev[j]+1,prev[j-1]+(x!=y)))
        prev=row
    wer=prev[-1]/max(1,len(a))
    # Exact numeric and telephone-key words protect against small-WER but material errors.
    number_words=set('ноль один одна два две три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто сто двести триста четыреста пятьсот шестьсот семьсот восемьсот девятьсот тысяча тысячи тысяч'.split())
    critical_a=[t for t in a if t in number_words];critical_b=[t for t in b if t in number_words]
    return {'wer':round(wer,4),'numbers_match':critical_a==critical_b,'passed':wer<=.15 and critical_a==critical_b}
