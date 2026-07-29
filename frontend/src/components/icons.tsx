import type { SVGProps } from "react";

const base = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none" };

export function IconLink(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M9 15l6-6" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
      <path
        d="M10.5 6.5l1-1a3.5 3.5 0 015 5l-1.5 1.5"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinecap="round"
      />
      <path
        d="M13.5 17.5l-1 1a3.5 3.5 0 01-5-5l1.5-1.5"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinecap="round"
      />
    </svg>
  );
}

export function IconCookie(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth={1.6} />
      <circle cx="9.5" cy="10" r="0.9" fill="currentColor" />
      <circle cx="14" cy="9.5" r="0.9" fill="currentColor" />
      <circle cx="13.5" cy="14" r="0.9" fill="currentColor" />
      <circle cx="9.5" cy="14.5" r="0.9" fill="currentColor" />
    </svg>
  );
}

export function IconMusic(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path
        d="M9 17.5a2 2 0 100-4 2 2 0 000 4zM17 15.5a2 2 0 100-4 2 2 0 000 4z"
        stroke="currentColor"
        strokeWidth={1.6}
      />
      <path d="M11 17.5V6l8-1.5v9.5" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
    </svg>
  );
}

export function IconFolder(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path
        d="M4 6.5a1 1 0 011-1h4.2l1.4 1.6H19a1 1 0 011 1V17a1 1 0 01-1 1H5a1 1 0 01-1-1V6.5z"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function IconDownload(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 4v11" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
      <path d="M7.5 11.5L12 16l4.5-4.5" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 19h14" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
    </svg>
  );
}

export function IconWave(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path
        d="M3 12h2l1.5-5L9 18l2-11 2 8 1.5-6L16 12h5"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function IconTranslate(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M4 6h8M8 4v2c0 4-2 7-6 8.5" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
      <path d="M5 9c1 2 3 3.3 5 4" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
      <path d="M14 20l3.5-9L21 20M15 17h5" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconVoice(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <rect x="9.5" y="3.5" width="5" height="9" rx="2.5" stroke="currentColor" strokeWidth={1.6} />
      <path d="M6 11a6 6 0 0012 0" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
      <path d="M12 17v3.5" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
    </svg>
  );
}

export function IconExport(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <rect x="3.5" y="5" width="17" height="12" rx="1.5" stroke="currentColor" strokeWidth={1.6} />
      <path d="M9.5 11l2.5-2.5L14.5 11M12 8.5V15" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconCheck(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M5 12.5l4.5 4.5L19 7" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconAlert(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 4L21 19H3L12 4z" stroke="currentColor" strokeWidth={1.6} strokeLinejoin="round" />
      <path d="M12 10v4" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" />
      <circle cx="12" cy="16.7" r="0.9" fill="currentColor" />
    </svg>
  );
}

export function IconClapper(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M4 10l1.3-4.2a1 1 0 011-.7l12.4 1.9a1 1 0 01.8 1.2L19 10" stroke="currentColor" strokeWidth={1.6} strokeLinejoin="round" />
      <path d="M6 8.3l11.8 2" stroke="currentColor" strokeWidth={1.4} />
      <rect x="4" y="10" width="16" height="9.5" rx="1.2" stroke="currentColor" strokeWidth={1.6} />
    </svg>
  );
}
