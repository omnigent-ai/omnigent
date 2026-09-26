import type { SVGProps } from "react";

/** GitLab's tanuki mark, rendered with the surrounding text color. */
export function GitlabIcon({ className, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      {...props}
    >
      <path
        fill="currentColor"
        fillRule="evenodd"
        d="M4.84 2.56a.72.72 0 0 1 1.37-.02L8.4 9.28h7.2l2.19-6.74a.72.72 0 0 1 1.37.02l3.31 10.19a2.1 2.1 0 0 1-.76 2.35l-9.29 6.75a.72.72 0 0 1-.84 0L2.29 15.1a2.1 2.1 0 0 1-.76-2.35L4.84 2.56Zm1.03 7.76-2.3 3.17a.65.65 0 0 0 .16.92l6.54 4.75-4.4-8.84Zm2.89.4L12 20.7l3.24-9.98H8.76Zm4.97 8.44 6.54-4.75a.65.65 0 0 0 .16-.92l-2.3-3.17-4.4 8.84Z"
        clipRule="evenodd"
      />
    </svg>
  );
}

export default GitlabIcon;
