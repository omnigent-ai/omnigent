export const normalizeSearchQuery = (value: string): string => {
  const trimmedValue = value.trim();
  if (trimmedValue.length === 0) {
    return "";
  }

  const normalizedSeparators = trimmedValue.includes("\\")
    ? trimmedValue.replaceAll("\\", "/")
    : trimmedValue;
  return normalizedSeparators.toLowerCase();
};
