# library/ — "Books In Stock"

Whatever book files you put in this folder show up automatically in the app
under **Books In Stock**, as tap-to-download cards. No code editing needed.

## How to add a book
1. On GitHub, open this `library` folder.
2. **Add file ▸ Upload files** → drag your file in → **Commit changes**.
3. Go to **Actions ▸ Run workflow** (the same step you use for schedules).
4. The book appears on the site after the build finishes.

## Naming → nice cards
The card title and author come from the file name:

| File name                              | Title                  | Author        |
|----------------------------------------|------------------------|---------------|
| `Operations Management - Heizer.pdf`   | Operations Management  | Heizer        |
| `SCM class notes.pdf`                  | SCM class notes        | —             |

So name files `Title - Author.ext` (a hyphen with spaces around it). Underscores
become spaces. Supported: pdf, epub, mobi, azw3, djvu, doc(x), ppt(x), txt, zip.

## Optional: colour-coded subject tag
Add a subject code in **[square brackets]** anywhere in the file name and the
card shows a coloured pill matching that subject's colour in the timetable grid:

| File name                                        | Card shows                |
|--------------------------------------------------|---------------------------|
| `Consumer Behavior - Schiffman [CB].pdf`         | CB pill (Marketing)       |
| `Financial Statement Analysis - Author [FSA].pdf`| FSA pill (Finance)        |

Valid codes are the abbreviations used in the timetable:
BI, BM, CB, ERP, FSA, I&PM, IB, IPM, MBC, MFS, PML, RMKT, S&DM, SBM, TQM.
Use the exact code (`I&PM` and `IPM` are different subjects). If you skip the
tag, the app still tries to match the title to a course name automatically; if
nothing matches, the card simply shows no pill.

## To remove a book
Delete the file from this folder and re-run the workflow.

## Please share responsibly
List only things you're free to distribute: your own notes/summaries,
open-access or public-domain books, past papers, or material a faculty member
has cleared. Don't upload scanned copies of copyrighted textbooks to this public
repo — for those, use the **Request a copy** form and point people to a library
or legal copy instead.
