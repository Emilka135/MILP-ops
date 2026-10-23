from datetime import date
import pytest
from clinic.identity import birth_date_input,normalize_birth_date

TODAY=date(2026,10,20)
@pytest.mark.parametrize('text,expected',[
    ('Двенадцатое 01.1995','1995-01-12'),('5 октября 1915','1915-10-05'),
    ('ноль три ноль пять 1996','1996-05-03'),('29.02.1904','1904-02-29'),
    ('1.1.1896','1896-01-01')])
def test_mixed_speech_and_centennarians(text,expected):
    assert normalize_birth_date(text,today=TODAY).isoformat()==expected

@pytest.mark.parametrize('text',['Ноль три, ноль пять девяносто шесть.','Двенадцатое 09.81','5 сентября восьмого года.','05.05.96'])
def test_short_year_requires_century_without_guessing(text):
    result=birth_date_input(text,today=TODAY)
    assert result['status']=='clarify' and result['reason']=='birth_year_required'
    assert result['field']['century_inference'] is False
    assert result['field']['minimum']=='0001-01-01' and result['field']['maximum']=='2026-10-20'

@pytest.mark.parametrize('text',['Девятнадцатое, 2028, четвёрты','31.02.1990','29.02.1900','2028-01-01'])
def test_impossible_future_or_scrambled_dates_never_search(text):
    assert birth_date_input(text,today=TODAY)['status']=='clarify'

def test_explicit_field_lower_bound_is_inclusive():
    assert birth_date_input('05.10.1915',today=TODAY,minimum=date(1915,10,5))['status']=='valid'
    assert birth_date_input('04.10.1915',today=TODAY,minimum=date(1915,10,5))['reason']=='birth_date_before_minimum'
