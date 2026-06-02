# Site Workflow

AISMixer's public website is deployed through GitHub Pages from the `website`
branch, using the `/docs` folder as the site root.

The `docs/` directory is the public representative website. Public technical
documentation may live under `docs/technical/` and can use Jekyll front matter
for rendered pages.

The `main` branch remains the primary code branch. Production Python changes
should normally be made on `main`, not on `website`, unless explicitly requested.
