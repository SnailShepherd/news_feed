from bs4 import BeautifulSoup
def soup_html(text):
    return BeautifulSoup(text, "lxml")
