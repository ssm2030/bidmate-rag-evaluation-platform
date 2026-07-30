import type { NextConfig } from "next";
import path from "path";

const reviewApiOrigin = process.env.EVAL_REVIEW_API_ORIGIN ?? "http://127.0.0.1:8101";

const nextConfig: NextConfig = {
  // Next.js detects the parent repo's package.json (kordoc CLI) as a workspace
  // root and prints a warning. Pin tracing root to this web/ directory.
  outputFileTracingRoot: path.join(__dirname),
  async rewrites() {
    return [
      {
        source: "/review-api/:path*",
        destination: `${reviewApiOrigin}/:path*`,
      },
      {
        source: "/api/:path*",
        destination: "http://localhost:8100/api/:path*",
      },
    ];
  },
};

export default nextConfig;
