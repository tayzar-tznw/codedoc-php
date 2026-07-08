<?php
declare(strict_types=1);

namespace App\S14_ConditionalTypes;

class CsvWriter
{
    public function write(array $rows): string
    {
        return 'csv:' . count($rows);
    }
}
