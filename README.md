# korean-isbn-scraper
Takes a list of ISBNs and finds each book on yes24, kyobo and aladin, first hit wins. You get the Korean title, original title if the store has one, author, publisher, list price (not the sale price) and the price converted to NZD. ISBN 10s get converted to 13 and a wrong check digit gets fixed. Run it on a text file with one ISBN per line, or use --gui and paste them into a window. Results are written to isbn_results.csv. No dependencies, curl is the backup if python gets an empty page.
<img width="1561" height="972" alt="image" src="https://github.com/user-attachments/assets/8b67c1f2-bcaf-49c2-b86d-b968a55b07c5" />
<img width="1554" height="960" alt="image" src="https://github.com/user-attachments/assets/f36dd66b-2bcc-49ad-a2ff-dd53ae67716a" />
