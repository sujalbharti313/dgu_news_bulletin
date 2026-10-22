export interface NavItem {
  label: string;
  href: string;
}

/** Section anchors are absolute ("/#...") so they work from every route. */
export const navItems: NavItem[] = [
  { label: "The Signal", href: "/#signal" },
  { label: "AI Tools", href: "/#tools" },
  { label: "Research", href: "/#research" },
  { label: "India × AI", href: "/#india" },
  { label: "Campus AI", href: "/#campus" },
  { label: "Archive", href: "/archive" },
];
