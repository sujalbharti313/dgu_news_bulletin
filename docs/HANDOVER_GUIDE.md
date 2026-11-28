# AI//CAMPUS: what to do when something stops working

This page is for the person looking after AI//CAMPUS. You do not need to know programming. Everything runs by
itself on free services; your weekly job is only to **review and publish the edition**.

## Every week (Monday)

1. On Monday the system prepares last week's edition (Monday to Sunday). It may appear a few hours late.
2. Open the website's **/admin** page, sign in with the editor account, open the new edition.
3. Read it. Edit anything wrong, then **Approve** and **Publish**. Nothing is ever published without you.

## How you find out about a problem

GitHub emails the owner of the repository with the subject "Run failed: AI//CAMPUS ...". Open the email and click
**View workflow run**. The red line tells you which part failed. Match it below.

## Problems and fixes

**"ACTION NEEDED: Google refused the Gmail sign-in" (Gmail stopped working)**
The TLDR AI newsletter can no longer be read. The rest of the news still arrives, so there is no hurry.
1. On the project computer open PowerShell, then type each line and press Enter:
   `cd $HOME\Documents\dgu_news_bulletin\backend` then `.venv\Scripts\Activate` then
   `$t = (python -m scripts.gmail_oauth)[-1]`
2. A browser opens. Sign in as **ainewsdgu@gmail.com**. If Google warns "not verified", click Advanced, then
   Continue. Allow access.
3. Back in PowerShell type `$t | Set-Clipboard`.
4. On github.com open the repository → **Settings → Secrets and variables → Actions** → `GMAIL_REFRESH_TOKEN` →
   **Update** → paste (Ctrl+V) → Update secret. Also open `backend\.env` in Notepad and replace the value after
   `GMAIL_REFRESH_TOKEN=` with the same text.
5. GitHub → **Actions → AI//CAMPUS live checks → Run workflow**. The Gmail lines should say PASS.

**The website shows "could not load" or is empty**
1. Open `https://<api-address>/api/v1/health`. If it says `"database":"ok"` wait 5 minutes and reload the website.
2. If it does not open, or says "degraded": open supabase.com, sign in, open the project. If it says **Paused**,
   click **Restore** and wait 2 minutes.
3. Still broken: Vercel → the project → **Deployments** → pick the last one marked Ready that worked →
   **⋯ → Promote to Production**.

**"Weekly edition generation failed" or no new edition on Monday**
Usually the free AI allowance was used up. Wait until the next day; it retries by itself. To try at once: GitHub →
**Actions → AI//CAMPUS pipeline → Run workflow** → choose `edition` → Run.

**"The scheduled workflow is disabled" email from GitHub**
GitHub pauses schedules after 60 days without changes. GitHub → **Actions** → AI//CAMPUS pipeline → **Enable
workflow**. Do the same for "AI//CAMPUS live checks".

**Cannot sign in to /admin**
There is no "forgot password" button. Supabase → **Authentication → Users** → the editor account → **Send password
recovery**, or ask the technical contact to reset it.

## Never do these

- Never share passwords, keys or tokens in chat, email or screenshots.
- Never turn on billing or "upgrade" on Vercel, Supabase, Google or GitHub. Everything is meant to stay free.
- Never delete the Supabase project or the GitHub repository.

Technical details: `docs/DEPLOYMENT.md`.
