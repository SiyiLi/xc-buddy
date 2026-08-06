# XC Buddy Website

This directory contains the Vue and Vite homepage and Web Serial flasher for
XC Buddy. The site supports Simplified Chinese and English.

The flasher reads the latest public GitHub Release and selects the exact
`xc-buddy-sticks3-merged.bin` asset. Set `VITE_FIRMWARE_URL` only when a local
or test build should override that release asset.

## Develop

```bash
npm ci
npm run dev
```

## Build

```bash
npm run build
```

Vite builds the site for the `/xc-buddy/` GitHub Pages path and copies
`public/appcast.xml` into the output. The macOS package can use that appcast by
setting `XC_BUDDY_APPCAST_URL` to its raw GitHub URL during the build.

Website deployment is not automated in the current repository. The flasher
requires a public Release containing the merged firmware asset; public firmware
publication remains controlled by the release gate documented in the root
README.
