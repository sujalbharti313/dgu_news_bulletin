import type { NextConfig } from "next";

/**
 * Security headers for every page (Phase 4.1 audit). They change nothing visible:
 * - the site, and above all the /admin pages that hold an editor's sign-in, cannot be framed by another site
 *   (clickjacking);
 * - browsers must not guess content types, and links to sources send only the site's origin as referrer;
 * - no page may use the camera, microphone or location.
 * A full script Content-Security-Policy is left for a later stage: it needs testing against the 3D scene.
 */
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
