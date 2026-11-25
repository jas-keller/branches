# How I host Flask apps on DreamHost

I host my Flask apps on a DreamHost Managed VPS. Each app has its own Python
virtual environment and runs as a separate Gunicorn process. DreamHost’s Proxy
Server connects each site’s public address to the process for that app. I use
the same pattern for my portfolio and family-history sites.

```text
Browser → DreamHost Proxy Server → Gunicorn → Flask app
```

DreamHost receives the web request and forwards it to Gunicorn. Gunicorn loads
the Flask app and passes the request to it. Flask runs the matching route, and
the response goes back through Gunicorn and DreamHost to the browser.
DreamHost offers its panel-managed proxy on Managed VPS and Dedicated plans.
[DreamHost’s Proxy Server documentation](https://help.dreamhost.com/hc/en-us/articles/217955787-Proxy-Server)

## Bringing a Flask app online

1. **Set up Python for the app.** I put a copy of the project on the VPS, create
   a virtual environment for it, and install the dependencies from
   `requirements.txt`. Keeping each environment separate lets each app use the
   package versions it needs.
2. **Run the app with Gunicorn.** Every project needs a Python entry point that
   exposes the Flask application. In this repo, `wsgi.py` calls `create_app()`
   and exposes the result as `app`, so the Gunicorn target is `wsgi:app`.
   [Flask’s Gunicorn guide](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/)
3. **Connect the public site.** In the DreamHost panel, I point the site’s proxy
   at the Gunicorn process for that app. Then I open the public site and check
   that the home page, a second route, and the page’s styles or images load.

## Updating a live app

When I release a change, I update the server’s copy of the code, refresh the
app’s Python dependencies if they changed, and restart that app’s Gunicorn
process so it loads the new version. Then I repeat the browser checks on the
public site. I can update one app without restarting the others.
