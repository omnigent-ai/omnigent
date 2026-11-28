import type { SVGProps } from "react";

// GitLab brand mark: https://github.com/devicons/devicon/blob/master/icons/gitlab/gitlab-original.svg
export function GitLabIcon({
  size = 24,
  ...props
}: SVGProps<SVGSVGElement> & { size?: number | string }) {
  return (
    <svg
      viewBox="0 0 128 128"
      width={size}
      height={size}
      fill="currentColor"
      aria-hidden="true"
      {...props}
    >
      <path d="m124.755 51.382-.177-.452L107.47 6.282a4.459 4.459 0 0 0-1.761-2.121 4.581 4.581 0 0 0-5.236.281 4.578 4.578 0 0 0-1.518 2.304L87.404 42.088H40.629L29.077 6.746a4.492 4.492 0 0 0-1.518-2.31 4.581 4.581 0 0 0-5.236-.281 4.502 4.502 0 0 0-1.761 2.121L3.422 50.904l-.17.452c-5.059 13.219-.763 28.192 10.537 36.716l.059.046.157.111 26.061 19.516 12.893 9.758 7.854 5.93a5.283 5.283 0 0 0 6.388 0l7.854-5.93 12.893-9.758 26.218-19.634.065-.052c11.273-8.526 15.562-23.472 10.524-36.677z" />
    </svg>
  );
}
