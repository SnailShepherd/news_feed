# -*- coding: utf-8 -*-
from dataclasses import dataclass
from typing import Optional, Pattern
import re

@dataclass
class Source:
    slug: str
    name: str
    mode: str  # 'rss' or 'html' or 'manual'
    url: str
    allowed_url_regex: Optional[Pattern] = None
    follow_detail_for_date: bool = False
    notes: str = ""

SOURCES: list[Source] = [
    Source(slug="notim", name="НОТИМ", mode="html",
           url="https://notim.ru/",
           follow_detail_for_date=True,
           notes="Парсим главную ленту; даты уточняем со страницы новости."),

    Source(slug="minstroyrf", name="Минстрой России", mode="html",
           url="https://minstroyrf.gov.ru/press/?d=news",
           follow_detail_for_date=True,
           notes="На '?d=all' ловили 401; раздел 'news' доступен анонимно."),

    Source(slug="ardexpert", name="АРД: статьи", mode="html",
           url="https://ardexpert.ru/article",
           follow_detail_for_date=True),

    Source(slug="gostinfo", name="Гостинформ", mode="html",
           url="https://www.gostinfo.ru/News/List",
           follow_detail_for_date=False),

    Source(slug="faufcc", name="ФАУ ФЦС", mode="html",
           url="https://faufcc.ru/press-tsentr/novosti",
           follow_detail_for_date=True,
           notes="Если 304 Not Modified — берём кэш предыдущей выборки."),

    Source(slug="ancb", name="АНЦБ", mode="html",
           url="https://ancb.ru/publication",
           follow_detail_for_date=True,
           notes="Переведено на HTTPS; раньше HTTP давал таймауты."),

    Source(slug="eec", name="ЕЭК ЕАЭС", mode="html",
           url="https://eec.eaeunion.org/news/",
           follow_detail_for_date=True),

    Source(slug="minfin", name="Минфин России", mode="html",
           url="https://minfin.gov.ru/ru/press-center/",
           follow_detail_for_date=True),

    Source(slug="interfax_realty", name="Интерфакс-Недвижимость",
           mode="html",
           url="https://www.interfax-russia.ru/realty/news",
           follow_detail_for_date=True),

    Source(slug="metalinfo", name="Металлоснабжение и сбыт",
           mode="rss",
           url="https://www.metalinfo.ru/ru/news/list.rss",
           allowed_url_regex=re.compile(r"^https?://(?:www\.)?metalinfo\.ru/ru/news/\d+$"),
           notes="RSS вместо обхода разделов; фильтруем категории/комментарии."),

    Source(slug="pnp", name="Парламентская газета: Экономика",
           mode="html",
           url="https://www.pnp.ru/economics/",
           follow_detail_for_date=True),

    Source(slug="erzrf", name="ЕРЗ.РФ",
           mode="html",
           url="https://erzrf.ru/news/",
           follow_detail_for_date=True,
           notes="Сайт редиректит; для дат лезем в карточку."),

    Source(slug="ria_stk", name="РИА СТК",
           mode="manual",
           url="https://ria-stk.ru/news/vse-novosti.php",
           notes="В GH часто Network unreachable. При необходимости добавлять вручную."),

    Source(slug="government", name="Правительство РФ",
           mode="html",
           url="http://government.ru/news/",
           follow_detail_for_date=True),

    Source(slug="stroygaz", name="Стройгаз.ру",
           mode="html",
           url="https://stroygaz.ru/news/",
           follow_detail_for_date=True),

    Source(slug="ria_realty", name="РИА Недвижимость: лента",
           mode="html",
           url="https://realty.ria.ru/lenta/",
           follow_detail_for_date=False),

    Source(slug="rg", name="Российская газета: Экономика",
           mode="html",
           url="https://rg.ru/tema/ekonomika",
           follow_detail_for_date=True),

    Source(slug="gge", name="Главгосэкспертиза",
           mode="rss",
           url="https://gge.ru/rss/",
           notes="RSS обходит 403 для GitHub Actions."),
]
