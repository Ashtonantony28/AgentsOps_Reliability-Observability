type Props = {
  title: string;
  hint?: string;
  action?: React.ReactNode;
};

export default function EmptyState({ title, hint, action }: Props) {
  return (
    <div className="border border-dashed border-ink-700 rounded-lg p-8 text-center text-ink-300">
      <div className="text-base font-medium">{title}</div>
      {hint && <div className="text-xs text-ink-400 mt-1">{hint}</div>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}
