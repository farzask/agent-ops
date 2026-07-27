/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone output keeps the Docker image small (see frontend/Dockerfile).
  output: "standalone",
};

export default nextConfig;
