<?php
declare(strict_types=1);

namespace App\S14_ConditionalTypes;

class ConditionalConsumer
{
    public function export(bool $asCsv, array $rows): string
    {
        $writer = $asCsv ? new CsvWriter() : new JsonWriter();

        return $writer->write($rows);
    }

    public function exportBy(string $format, array $rows): string
    {
        $writer = match ($format) {
            'csv' => new CsvWriter(),
            default => new JsonWriter(),
        };

        return $writer->write($rows);
    }
}
