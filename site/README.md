# settag.dev

The SetTag homepage. TanStack Start, prerendered to static HTML.

```bash
pnpm install
pnpm dev      # http://localhost:3000
pnpm build    # writes dist/client/
```

## Why it is prerendered

`vite.config.ts` sets `prerender.enabled`, so `pnpm build` emits finished
HTML into `dist/client/`. `wrangler.jsonc` has no `main`, so the Worker is
assets only and runs no code. If a page ever needs a server function, remove the
prerender block, add the Cloudflare Workers adapter, and give `wrangler.jsonc` a
`main` — the
[hosting guide](https://tanstack.com/start/latest/docs/framework/react/guide/hosting)
covers the change.

## Deploying

Wrangler owns the Worker and its assets. Terraform owns the hostname, in the
`lsdcapital/terraform` repo under `settag/cloudflare`. Do not add `routes` or
`custom_domain` to `wrangler.jsonc` — that association belongs to Terraform.

```sh
pnpm run deploy
```

Use `pnpm run deploy`, never bare `pnpm deploy` — `deploy` is also a built-in
pnpm subcommand, so the bare form is ambiguous.

The script wraps wrangler in `doppler run --project settag --config prod`
itself, rather than expecting you to. Doppler `settag/prod` holds
`CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`, the two variables wrangler
reads. Without them wrangler does not fail — it silently deploys to whichever
account the logged-in user defaults to, which is not the one owning
`settag.dev`. The Worker lands somewhere Terraform cannot see, and
`make apply STACK=settag/cloudflare` fails with 10007 "This Worker does not
exist on your account".

The scope is passed explicitly because no `doppler setup` scope is configured
for this directory, and relying on one would make the command work on some
machines and not others.

The first deploy also needs the hostname wired up, once:

1. `pnpm run deploy`, creating the `settag-web` Worker in the right account.
2. Set `TF_VAR_SETTAG_WORKER_NAME=settag-web` in Doppler, project `settag`,
   config `prod`. The stack gates its custom domain on that variable and stays a
   no-op while it is empty. Set it in Doppler, not in your shell — an exported
   value works for one apply and then vanishes.
3. `make apply STACK=settag/cloudflare` from the terraform repo.

Verify with `pnpm run whoami`, which prints the account wrangler will actually
use. It must be the account owning `settag.dev`, not a personal one.

Order matters: `cloudflare_workers_custom_domain` resolves the Worker by name at
apply time, so the Worker must exist first. Applying early fails with 10007.

Later deploys are step 1 alone.

`.github/workflows/site.yml` builds and checks the output on pull requests. It
does not deploy.

## Design notes

Ochre and slate carry meaning inside the staged-changes panel: ochre is a value
SetTag proposes but has not written, slate is what is currently on disk.
Nothing else on the page uses those two colours, and the accent is tuned to
clear 4.5:1 on both background tones. Keep that rule if you add sections.

Track names in the example panel are invented. They are illustration, not real
releases, and should stay that way.

## Licensing

This directory is covered by the repository's AGPL-3.0-only licence along with
everything else in the tree.
