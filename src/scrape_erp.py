import requests
from bs4 import BeautifulSoup
import re

# URL of the page
url = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/home.htm"  # Replace with the correct full URL if deeper than homepage

# Fetch the page
response = requests.get(url)
response.raise_for_status()  # Raises an error if the request failed

# Parse the HTML
soup = BeautifulSoup(response.text, "html.parser")

# Find the paragraph containing the ERP info
paragraphs = soup.find_all("p")
for p in paragraphs:
    if "Implied ERP" in p.get_text():
        text = p.get_text()
        break
else:
    raise ValueError("Couldn't find the paragraph with Implied ERP")

# Use regex to extract the first percentage value
match = re.search(r"(\d+\.\d+)%", text)
if match:
    implied_erp = match.group(1)
    print(f"Implied ERP: {implied_erp}%")
else:
    print("Couldn't extract Implied ERP value")
