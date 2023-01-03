import os
import pandas as pd
import csv
import html2text
import sys
import requests
from atlassian import Confluence
import openai
from pprint import pprint
from bs4 import BeautifulSoup
import argparse
from transformers import GPT2TokenizerFast
from typing import Tuple
from nltk.tokenize import sent_tokenize


# Create an ArgumentParser object
parser = argparse.ArgumentParser()

# Add an argument with a flag and a name
parser.add_argument("--spaces", nargs="*", default=["STRM"], help="Specify the Confluence Space you want to index")
parser.add_argument("--zendesk", nargs="*", default=["learningpool"], help="Specify the Zendesk domains you want to index")
parser.add_argument("--max_pages", default=1000, help="The maximum amount of Space pages to index")
parser.add_argument("--out", default="indexed_content", help="Specify the filename to save the content")
parser.add_argument("--min_tokens", default=20, help="Remove content with less than this number of tokens")

args = parser.parse_args()
max_pages = args.max_pages

# Connect to Confluence
confluence = Confluence(url='https://learninglocker.atlassian.net', username=os.environ.get('CONFLUENCE_USERNAME'), password=os.environ.get('CONFLUENCE_API_KEY'))

tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")

def count_tokens(text: str) -> int:
    """count the number of tokens in a string"""
    return len(tokenizer.encode(text))

def reduce_long(
    long_text: str, long_text_tokens: bool = False, max_len: int = 590
) -> str:
    """
    Reduce a long text to a maximum of `max_len` tokens by potentially cutting at a sentence end
    """
    if not long_text_tokens:
        long_text_tokens = count_tokens(long_text)
    if long_text_tokens > max_len:
        sentences = sent_tokenize(long_text.replace("\n", " "))
        ntokens = 0
        for i, sentence in enumerate(sentences):
            ntokens += 1 + count_tokens(sentence)
            if ntokens > max_len:
                return ". ".join(sentences[:i][:-1]) + "."

    return long_text


def extract_html_content(
  title_prefix: str,
  page_title: str,
  html: str
):
  ntitles, nheadings, ncontents = [], [], []

  soup = BeautifulSoup(html, 'html.parser')
  headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])

  prev_heading = []

  # Iterate through all headings and subheadings
  for h in headings:
    # Extract the heading text and remove HTML
    heading = html2text.html2text(str(h)).strip()

    # Initialize the content list
    content = []

    # Find the next heading or subheading
    next_h = h.find_next(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])

    actual_heading = heading.lstrip('#').lstrip(' ')

    # Iterate through all siblings until the next heading or subheading is reached
    for sibling in h.next_siblings:
      if sibling == next_h:
        break

      # If the sibling is a tag, extract the text and remove HTML
      if sibling.name:
        para = html2text.html2text(str(sibling)).strip()
        if len(para) > 0:
          content.append(para)

    # If there are content entries, join them all together, clean up for utf-8 and write the row
    if len(content) > 0:
      content = "".join(content).replace("\n", "").encode('utf-8').decode('utf-8')

      # If there are headings above this one without content, we concat them here
      if len(prev_heading) > 0:
        full_heading = " - ".join(prev_heading) + " - " + actual_heading
      else:
        full_heading = actual_heading

      title = f"{title_prefix} - {page_title}"
      # Store the extracted title, heading, content
      ntitles.append(title)
      nheadings.append(full_heading)
      ncontents.append(f"{title} - {full_heading} - {content}")
      prev_heading = []
    else:
      # Otherwise, we store this heading to append to the next sibling with content
      prev_heading.append(actual_heading)
  
  # Return the 3 arrays of titles, headings and content
  return (ntitles, nheadings, ncontents)

def count_content_tokens(
  ntitles: list,
  nheadings:list,
  ncontents: list
):
  # count the tokens of each section
  ncontent_ntokens = [
      count_tokens(c) # Add the tokens from the content
      + 4
      + count_tokens(" ".join(t.split(" ")[1:-1])) # Add the tokens from the titles
      + count_tokens(" ".join(h.split(" ")[1:-1])) # Add the tokens from the headings
      - (1 if len(c) == 0 else 0)
      for t, h, c in zip(ntitles, nheadings, ncontents)
  ]
  # Create a tuple of (title, section_name, content, number of tokens)
  outputs = []
  outputs += [(t, h, c, tk) if tk<max_len 
              else (h, reduce_long(c, max_len), count_tokens(reduce_long(c,max_len))) 
                  for t, h, c, tk in zip(ntitles, nheadings, ncontents, ncontent_ntokens)]
  return outputs


def extract_sections(
  space: str,
  limit: int = max_pages
):
  ntitles, nheadings, ncontents = [], [], []

  confluence_space = confluence.get_space(space_key=space)
  space_title = confluence_space['name']

  print(f"Fetching up to {limit} pages from '{space_title}'...")

  # Search for all pages in a given space
  results = confluence.get_all_pages_from_space(space=space, start=0, limit=limit)

  page_ids = []
  for result in results:
      page_ids.append(result["id"])

  # Iterate through the list of Confluence pages
  for page_id in page_ids:
      # Fetch the Confluence page
      page = confluence.get_page_by_id(page_id=page_id, expand="body.storage")

      # Extract the page title and content
      page_title = page['title']
      page_html = page['body']['storage']['value']
      
      pageTitles, pageHeadings, pageContent = extract_html_content(space_title, page_title, page_html)
      ntitles += pageTitles
      nheadings += pageHeadings
      ncontents += pageContent

  return count_content_tokens(ntitles, nheadings, ncontents) 


def extract_zendesk_domain(
  zendesk_domain: str
):
  ntitles, nheadings, ncontents = [], [], []

  URL = f"https://{zendesk_domain}.zendesk.com/api/v2/help_center/en-us"

  # Fetch the Categories from Zendesk
  cat_response = requests.get(URL + '/categories.json')
  cat_data = cat_response.json()
  for category in cat_data['categories']:
    category_title = category['name']

    # Fetch the sections within the categories
    sections_response = requests.get(URL + '/categories/' + str(category['id']) + '/sections.json')
    sections_data = sections_response.json()
    for section in sections_data['sections']:
      page_title = section['name']
      
      # Fetch the articles within the section
      pprint(URL + '/section/' + str(section['id']) + '/articles.json')
      articles_response = requests.get(URL + '/sections/' + str(section['id']) + '/articles.json')
      articles_data = articles_response.json()

      for article in articles_data["articles"]:
        page_title += " - " + article['title']
        page_html = article['body']

        if (page_html is not None):
          pageTitles, pageHeadings, pageContent = extract_html_content(category_title, page_title, page_html)
          ntitles += pageTitles
          nheadings += pageHeadings
          ncontents += pageContent
      
      if (articles_data['next_page'] is not None):
        pprint('TODO! But have not seen multiple pages yet at this level (due to using sections...)')
  
  return count_content_tokens(ntitles, nheadings, ncontents)  


# Define the maximum number of tokens we allow per row
max_len = 1500

# For each Space, fetch the content and add to a list(title, heading, content, tokens)
res = []

for space in args.spaces:
  res += extract_sections(space)

for domain in args.zendesk:
  res += extract_zendesk_domain(domain)

# Remove rows with less than 40 tokens
df = pd.DataFrame(res, columns=["title", "heading", "content", "tokens"])
df = df[df.tokens > args.min_tokens]
df = df.drop_duplicates(['title','heading'])
df = df.reset_index().drop('index',axis=1) # reset index
print(df.head())

# Store the content to a CSV
dir = 'output/';
filename = args.out + '.csv'
fullpath = dir + filename
df.to_csv(fullpath, index=False)

print(f"Done! File saved to {fullpath}")